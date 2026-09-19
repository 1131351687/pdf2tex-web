#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compile LaTeX in an ASCII staging directory and emit a QA report.

2026-09 加固（原版两处缺陷）：
  - subprocess timeout 未捕获：编译卡死会抛 TimeoutExpired 直接崩溃整条流水线，
    而非作为一次失败上报。现在捕获并记 timed_out=true。
  - 缺字只是 .log 里的 warning：字形静默回退（tofu）时 returncode 仍为 0、
    errors 为空，于是 passed=true。现在解析 "Missing character" 行，把缺失字形
    与受影响字体量化进报告，供质量门判定（CJK 文档里非零即强烈 tofu 信号）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

from pypdf import PdfReader

# 形如：Missing character: There is no 第 (U+7B2C) in font [lmroman10-regular]...
# （该行在 .log 里可能折行，但本正则在折行前即匹配完毕。）
GLYPH_RE = re.compile(
    r"Missing character: There is no (.) \(U\+([0-9A-Fa-f]+)\) in font \[([^\]]+)\]"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tex", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    tex = args.tex.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if not tex.is_file():
        parser.error(f"TeX file not found: {tex}")

    with tempfile.TemporaryDirectory(prefix="pdf2tex-") as temporary:
        stage = Path(temporary)
        shutil.copy2(tex, stage / tex.name)
        images = tex.parent / "images"
        if images.is_dir():
            shutil.copytree(images, stage / "images", dirs_exist_ok=True)
        started = time.perf_counter()
        completed = None
        timed_out = False
        timeout_stdout = ""
        try:
            completed = subprocess.run(
                ["xelatex", "-interaction=nonstopmode", tex.name],
                cwd=stage,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=600,
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            timeout_stdout = exc.stdout or ""
            if isinstance(timeout_stdout, bytes):
                timeout_stdout = timeout_stdout.decode("utf-8", "replace")
        elapsed = round(time.perf_counter() - started, 3)
        stem = tex.stem
        log = stage / f"{stem}.log"
        pdf = stage / f"{stem}.pdf"
        log_text = ""
        if log.exists():
            shutil.copy2(log, args.output / log.name)
            log_text = log.read_text(encoding="utf-8", errors="replace")
        elif completed is not None:
            log_text = completed.stdout
        else:
            log_text = timeout_stdout
        pages = 0
        if pdf.exists():
            shutil.copy2(pdf, args.output / pdf.name)
            try:
                pages = len(PdfReader(str(pdf)).pages)
            except Exception:
                pages = 0

    errors = re.findall(r"^! (.+)$", log_text, flags=re.M)
    glyph_hits = GLYPH_RE.findall(log_text)
    distinct_glyphs: "OrderedDict[str, str]" = OrderedDict()
    distinct_fonts: "OrderedDict[str, None]" = OrderedDict()
    for ch, cp, font in glyph_hits:
        distinct_glyphs.setdefault(ch, cp)
        distinct_fonts[font] = None

    returncode = completed.returncode if completed is not None else -1
    passed = (not timed_out) and returncode == 0 and not errors
    report = {
        "tex": str(tex),
        "returncode": returncode,
        "seconds": elapsed,
        "timed_out": timed_out,
        "preview_pdf_pages": pages,
        "error_count": len(errors),
        "errors": errors[:50],
        "missing_glyph_count": len(glyph_hits),
        "missing_glyphs": [
            {"char": ch, "codepoint": f"U+{cp.upper()}"}
            for ch, cp in list(distinct_glyphs.items())[:50]
        ],
        "missing_glyph_fonts": list(distinct_fonts.keys())[:20],
        "passed": passed,
        "note": (
            "timed_out: compilation exceeded 600s; review the source for an "
            "infinite macro loop."
            if timed_out
            else (
                "missing_glyph_count>0 means glyphs are falling back to .notdef "
                "(tofu boxes); passed only checks that it compiled. For CJK "
                "material, treat a non-zero count as a font-coverage failure."
                if glyph_hits
                else "A preview PDF may exist despite errors; passed=false means "
                "manual review is required."
            )
        ),
    }
    (args.output / "compile-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

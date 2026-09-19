#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用指定的 Markdown 片段替换两个唯一正则锚点之间的区域。

这是修 OCR 串页/漏页时的“小手术”工具，不负责理解正文：

* 起始锚点和结束锚点都必须各命中一次，且顺序正确；
* 默认先做 dry-run，输出替换前后行数、字符数和差异摘要；
* 真正写入原文件时自动生成带时间戳的备份；
* 可选写出 JSON 报告，记录输入、输出、锚点、哈希和备份路径。

示例：
  python splice_markdown_section.py book.md repair.md out.md \
      --start-regex '^如果在射影平面上考虑一条形状如双曲线.*$' \
      --end-regex '^## §4\\. 组合方法\\s*$' \
      --report splice.json --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path: Path) -> tuple[str, bool, str]:
    """返回 (正文, 是否有 BOM, 换行风格)。"""
    raw = path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text, has_bom, newline


def write_text(path: Path, text: str, has_bom: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = text.encode("utf-8")
    if has_bom:
        encoded = b"\xef\xbb\xbf" + encoded
    path.write_bytes(encoded)


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def match_info(pattern: str, text: str, label: str) -> tuple[re.Match[str], int]:
    try:
        regex = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise ValueError(f"{label}正则无效: {exc}") from exc
    matches = list(regex.finditer(text))
    if len(matches) != 1:
        positions = [line_number(text, m.start()) for m in matches[:10]]
        raise ValueError(
            f"{label}必须唯一命中，实际命中 {len(matches)} 次"
            + (f"，行号: {positions}" if positions else "")
        )
    return matches[0], len(matches)


def normalize_replacement(replacement: str, newline: str) -> str:
    """统一换行，并保证替换块与边界之间保留 Markdown 段落空行。"""
    replacement = replacement.replace("\r\n", "\n").replace("\r", "\n")
    if not replacement:
        return ""
    replacement = replacement.strip("\n")
    return replacement.replace("\n", newline) + newline * 2


def backup_path_for(source: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = source.with_name(f"{source.name}.bak-{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = source.with_name(f"{source.name}.bak-{stamp}-{suffix}")
        suffix += 1
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="原 Markdown")
    parser.add_argument("replacement", type=Path, help="替换片段（UTF-8）")
    parser.add_argument("output", type=Path, help="输出 Markdown")
    parser.add_argument("--start-regex", required=True, help="起始锚点（包含）")
    parser.add_argument("--end-regex", required=True, help="结束锚点（不替换）")
    parser.add_argument("--report", type=Path, help="可选 JSON 报告")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不写文件")
    args = parser.parse_args()

    source = args.source.resolve()
    replacement_path = args.replacement.resolve()
    output = args.output.resolve()
    if not source.is_file():
        parser.error(f"找不到原 Markdown: {source}")
    if not replacement_path.is_file():
        parser.error(f"找不到替换片段: {replacement_path}")

    original, has_bom, newline = read_text(source)
    replacement, _, _ = read_text(replacement_path)
    start_match, _ = match_info(args.start_regex, original, "起始锚点")
    end_match, _ = match_info(args.end_regex, original, "结束锚点")
    if end_match.start() <= start_match.start():
        raise ValueError("结束锚点必须位于起始锚点之后")

    replacement = normalize_replacement(replacement, newline)
    updated = (
        original[:start_match.start()]
        + replacement
        + original[end_match.start():]
    )

    start_line = line_number(original, start_match.start())
    end_line = line_number(original, end_match.start())
    before_lines = original.count("\n") + (0 if original.endswith("\n") else 1)
    after_lines = updated.count("\n") + (0 if updated.endswith("\n") else 1)
    changed = original != updated

    backup: Path | None = None
    written = False
    if changed and not args.dry_run:
        if output == source:
            backup = backup_path_for(source)
            write_text(backup, original, has_bom)
        write_text(output, updated, has_bom)
        written = True

    report = {
        "schema": 1,
        "source": str(source),
        "replacement": str(replacement_path),
        "output": str(output),
        "dry_run": args.dry_run,
        "changed": changed,
        "written": written,
        "start_anchor": args.start_regex,
        "end_anchor": args.end_regex,
        "start_line": start_line,
        "end_line": end_line,
        "replaced_line_span": [start_line, end_line - 1],
        "source_sha256": sha256_text(original),
        "replacement_sha256": sha256_text(replacement),
        "output_sha256": sha256_text(updated),
        "line_count": {"before": before_lines, "after": after_lines},
        "char_count": {"before": len(original), "after": len(updated)},
        "backup": str(backup) if backup else None,
    }

    print(f"原文件: {source}")
    print(f"替换块: {replacement_path}")
    print(f"锚点: 起始 L{start_line}，结束 L{end_line}")
    print(
        f"行数: {before_lines} -> {after_lines}；"
        f"字符: {len(original)} -> {len(updated)}；changed={changed}"
    )
    if args.dry_run:
        print("dry-run：未写入任何文件。")
    elif changed:
        print(f"已写入: {output}")
        if backup:
            print(f"备份: {backup}")
    else:
        print("内容无变化，未写入文件。")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"报告: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

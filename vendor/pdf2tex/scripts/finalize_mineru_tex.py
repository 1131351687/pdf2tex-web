#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply conservative portability fixes to MinerU's generated LaTeX.

重要适用范围（踩过的坑）：
  本脚本修的是 **MinerU 直出的 .tex**（该 .tex 自己带导言区、直接进 xelatex）。
  如果项目走的是 Markdown -> pandoc -> LaTeX 链条，导言区由 pandoc 重新生成，
  那么这里对导言区的修改（arydshln / setmainfont）**不会**出现在最终文档里。
  那种链条下的同类修复必须施加在 .md 源文件上。实测教训：
    - Markdown/pandoc 链中的 `\\hdashline` 在这里补 `\\usepackage{arydshln}` 是无效的，
      正确修法是在 .md 里把 `\\hdashline` 改成 `\\hline`；
    - 这里选的西里尔字体（Times New Roman）不含中文字形，中文书应当用 SimSun 等。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 结构性修复（重复 \tag、越界 array）由同目录的 repair_latex.py 负责，
# 免得两处各写一份实现而逐渐走样。
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from repair_latex import repair as repair_latex_structures
except ImportError:  # 单独拷走本文件时退化为不做结构性修复
    repair_latex_structures = None


def add_after(text: str, marker: str, addition: str) -> str:
    if addition.strip() in text:
        return text
    if marker not in text:
        raise ValueError(f"Expected preamble marker not found: {marker}")
    return text.replace(marker, marker + "\n" + addition, 1)


def add_after_line(text: str, line_marker: str, addition: str) -> str:
    """Insert `addition` on its own line right after the line containing line_marker."""
    if addition.strip() in text:
        return text
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line_marker in line:
            lines.insert(i + 1, addition + "\n")
            return "".join(lines)
    raise ValueError(f"Expected preamble line not found: {line_marker}")


def finalize(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    if "\\hdashline" in text and "{arydshln}" not in text:
        # arydshln must load AFTER longtable (it patches longtable's row
        # macros); loading it before makes \adl@@cr undefined inside tables.
        if re.search(r"\\usepackage\s*\{[^}]*longtable", text):
            text = add_after_line(
                text,
                re.search(r"\\usepackage\s*\{[^}]*longtable[^}]*\}", text).group(0),
                "\\usepackage{arydshln}",
            )
        else:
            text = add_after(text, "\\usepackage{booktabs}", "\\usepackage{arydshln}")
        changes.append("added arydshln for \\hdashline")

    # MinerU defines an English font family but does not make it the document's
    # main font. Latin Modern then drops Cyrillic and some Greek glyphs.
    # 注意：这里选的字体不含中文字形，中文文档请改用 SimSun/SimHei 等。
    if any("\u0400" <= char <= "\u04ff" for char in text) and "\\setmainfont" not in text:
        font_setup = r"""\IfFontExistsTF{Times New Roman}
  {\setmainfont{Times New Roman}}
  {\IfFontExistsTF{DejaVu Serif}
    {\setmainfont{DejaVu Serif}}
    {\setmainfont{Arial}}}"""
        text = add_after(text, "\\usepackage{fontspec}", font_setup)
        changes.append("selected a Cyrillic-capable main font")

    # VLM occasionally repeats a determinant's all-dot placeholder row beyond
    # the declared array width. Trimming only homogeneous dot rows is safe and
    # avoids modifying mathematical data rows.
    arrays_fixed = 0

    def fix_array(match: re.Match[str]) -> str:
        nonlocal arrays_fixed
        spec, body = match.group(1), match.group(2)
        columns = len(re.findall(r"[clr]", spec))
        if not columns:
            return match.group(0)
        rows = re.split(r"(\\\\)", body)
        for index in range(0, len(rows), 2):
            cells = rows[index].split("&")
            normalized = {cell.strip() for cell in cells}
            dots = {r"\cdot", r"\dots", r"\ldots", r"\cdots", "."}
            if len(cells) > columns and normalized <= dots:
                rows[index] = " & ".join(cell.strip() for cell in cells[:columns])
                arrays_fixed += 1
        return f"\\begin{{array}}{{{spec}}}{''.join(rows)}\\end{{array}}"

    text = re.sub(
        r"\\begin\{array\}\{([^}]*)\}(.*?)\\end\{array\}",
        fix_array,
        text,
        flags=re.S,
    )
    if arrays_fixed:
        changes.append(f"trimmed {arrays_fixed} over-wide all-dot array row(s)")

    # 结构性修复：这两类会让 xelatex 直接中止，而上面的纯点号行裁剪覆盖不到
    # 真实数据行，此前无人负责，故重复 \tag 与越界 array 都会漏到编译阶段。
    if repair_latex_structures is not None:
        text, report = repair_latex_structures(text, keep="last")
        changes.extend(report["tag_fixes"])
        changes.extend(report["array_fixes"])

    return text, changes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    text = args.input.read_text(encoding="utf-8")
    text, changes = finalize(text)
    args.output.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.output}")
    for change in changes:
        print(f"- {change}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 MinerU markdown 里的原生 HTML <table>（含 colspan/rowspan）解析为
绝对列对齐结构，并重建 列不错位、保留跨列/跨行 的 LaTeX longtable，
替换 MinerU HTML->TeX 转换中错位的多级表头表。

用法:
  python fix_html_tables.py <input.md>                     # 诊断：打印每张表网格
  python fix_html_tables.py <input.md> --patch-tex <out.tex>  # 原位替换 tex 里第K张表
  python fix_html_tables.py <input.md> --patch-md <out.md>    # 转成 Markdown 管道表

2026-09 修复（原 _esc 是恒等死桩，从未被调用）：
  - 单元格文本进 LaTeX 前做【感知数学模式】的转义：$...$ 内原样保留，
    数学模式之外转义 \\ & % $ # _ { } ~ ^。
    实测真实故障：OCR 表格角标写成 x\\y，进入 tabular 后 \\y 被当成未定义命令，
    XeLaTeX 报 "Undefined control sequence"。注意不能无脑转义——表格里多数反斜杠
    是 $\\overline{a}$、$\\frac{1}{2}$ 这类合法数学，误转会破坏公式。
  - --patch-tex 原本按序号 min() 配对，张数不匹配时会静默改错块；
    现在张数不等即报错，需显式 --force 才继续。
  - 诊断模式会列出「数学模式外的裸反斜杠」单元格，便于先修数据再编译。
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
TD = re.compile(r"<td([^>]*)>(.*?)</td>", re.S)
TABLE_RE = re.compile(r"<table>.*?</table>", re.S)
MATH_SPAN = re.compile(r"(\$[^$]*\$)")

LATEX_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def esc_latex(text: str, enabled: bool = True) -> str:
    """感知数学模式地转义 LaTeX 特殊字符。

    $...$ 片段原样保留（其中的 _ ^ \\ { } 都是数学语法，转义即破坏公式）；
    其余片段逐字符转义。
    """
    if not enabled:
        return text
    out: list[str] = []
    for seg in MATH_SPAN.split(text):
        if len(seg) >= 2 and seg.startswith("$") and seg.endswith("$"):
            out.append(seg)
        else:
            out.append("".join(LATEX_SPECIAL.get(ch, ch) for ch in seg))
    return "".join(out)


def bare_backslashes(text: str) -> list[str]:
    """返回数学模式之外的裸反斜杠片段（潜在 Undefined control sequence）。"""
    bad: list[str] = []
    for seg in MATH_SPAN.split(text):
        if len(seg) >= 2 and seg.startswith("$") and seg.endswith("$"):
            continue
        if re.search(r"\\[A-Za-z]", seg):
            bad.append(seg.strip())
    return bad


def cell_text(raw: str) -> str:
    t = re.sub(r"<[^>]+>", "", raw)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
         .replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"[ \t]+", " ", t).strip()


def attrs(tag: str) -> dict:
    return {k.lower(): v for k, v in re.findall(r'([A-Za-z]+)\s*=\s*"([^"]*)"', tag)}


def parse_html_table(html: str):
    """返回 (rows_cells, grid, R, C)。"""
    trs = TR.findall(html)
    if not trs:
        return None
    rows_cells = []
    grid: list[list[str | None]] = []
    cols = 0
    pending_next: dict[int, int] = {}
    for tr in trs:
        tds = TD.findall(tr)
        incoming = dict(pending_next)
        pending_next = {}
        row: dict[int, str | None] = {}
        row_info = []
        col = 0
        for tag, content in tds:
            a = attrs(tag)
            cs = int(a.get("colspan", 1) or 1)
            rs = int(a.get("rowspan", 1) or 1)
            while col in incoming:
                row[col] = None
                col += 1
            start_col = col
            txt = cell_text(content)
            row[col] = txt
            for k in range(1, cs):
                row[col + k] = None
            if rs > 1:
                for k in range(cs):
                    pending_next[col + k] = max(pending_next.get(col + k, 0), rs - 1)
            row_info.append({"col": start_col, "text": txt, "colspan": cs, "rowspan": rs})
            col += cs
        for c in sorted(incoming):
            if c >= col and c not in row:
                row[c] = None
        this_cols = max([c + 1 for c in row]) if row else 0
        cols = max(cols, this_cols)
        grid.append([row.get(c) for c in range(this_cols)])
        rows_cells.append(row_info)
    for r in range(len(grid)):
        if len(grid[r]) < cols:
            grid[r] = grid[r] + [None] * (cols - len(grid[r]))
    return rows_cells, grid, len(grid), cols


def _esc(t: str) -> str:
    """保留旧名以兼容外部调用；现为真正的感知数学模式转义。"""
    return esc_latex(t)


def build_longtable(rows_cells, grid, R: int, C: int, escape: bool = True) -> str:
    """从展平网格重建 longtable：每行固定输出 C 个物理格。
    rowspan 覆盖列与 colspan 延续列输出空串，保证列号对齐不错位。"""
    colspec = "l" * C
    lines = ["\\begin{longtable}{@{}" + colspec + "@{}}", "\\toprule"]
    for r in range(R):
        row = grid[r]
        cells_out = ["" if c is None else esc_latex(c, escape) for c in row]
        lines.append(" & ".join(cells_out) + " \\\\")
    lines.append("\\bottomrule")
    lines.append("\\end{longtable}")
    return "\n".join(lines)


def build_markdown(grid, R: int, C: int) -> str:
    """从展平网格重建对齐的 Markdown 管道表。每行 C 格，None 留空。"""
    lines = []
    for r in range(R):
        row = grid[r]
        cells = [("" if c is None else c) for c in row]
        cells = [c.replace("|", "\\|") for c in cells]
        lines.append("| " + " | ".join(cells) + " |")
        if r == 0:
            lines.append("|" + "---|" * C)
    return "\n".join(lines)


def report_hazards(tables: list[str]) -> int:
    total = 0
    for i, t in enumerate(tables):
        p = parse_html_table(t)
        if not p:
            continue
        _rows, grid, R, _C = p
        for r in range(R):
            for c in grid[r]:
                if c is None:
                    continue
                bad = bare_backslashes(c)
                if bad:
                    total += 1
                    print(f"  ! table{i} r{r}: 数学模式外裸反斜杠 -> {bad}")
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("--patch-tex", type=Path, default=None)
    ap.add_argument("--patch-md", type=Path, default=None)
    ap.add_argument("--no-escape", action="store_true",
                    help="关闭单元格 LaTeX 转义（默认开启）")
    ap.add_argument("--force", action="store_true",
                    help="--patch-tex 张数与 md 表数不一致时仍按序号配对")
    args = ap.parse_args()
    md = args.input.read_text(encoding="utf-8")
    tables = TABLE_RE.findall(md)
    escape = not args.no_escape

    if args.patch_tex is None and args.patch_md is None:
        for i, t in enumerate(tables):
            p = parse_html_table(t)
            if not p:
                print(f"--- table {i}: parse failed")
                continue
            rows_cells, grid, R, C = p
            print(f"--- table {i}: {R} rows x {C} cols")
            for r in range(R):
                if r < 14:
                    print("  r%d: %s" % (r, " | ".join(("·" if c is None else c) for c in grid[r])))
        print("\n--- 裸反斜杠隐患 ---")
        n = report_hazards(tables)
        print(f"  合计 {n} 处" if n else "  无")
        return 0

    if args.patch_md is not None:
        for m in reversed(list(TABLE_RE.finditer(md))):
            p = parse_html_table(m.group(0))
            if not p:
                continue
            rows_cells, grid, R, C = p
            md = md[:m.start()] + build_markdown(grid, R, C) + md[m.end():]
        Path(args.patch_md).write_text(md, encoding="utf-8")
        print(f"patched md -> {args.patch_md} ({len(tables)} tables)")
        n = report_hazards(tables)
        if n:
            print(f"! 注意: 源表中有 {n} 处数学模式外裸反斜杠，转成 Markdown 后若再经 "
                  f"pandoc 转 LaTeX 仍会触发未定义命令，建议先修正源数据。")
        return 0

    tex_path = args.patch_tex
    tex = tex_path.read_text(encoding="utf-8")
    block_pat = re.compile(
        r"\\begin\{longtable\}[^\n]*\n.*?\\end\{longtable\}|"
        r"\\begin\{tabular\}[^\n]*\n.*?\\end\{tabular\}",
        re.S,
    )
    tex_blocks = list(block_pat.finditer(tex))
    print(f"tex has {len(tex_blocks)} table block(s), md has {len(tables)} table(s)")
    if len(tex_blocks) != len(tables):
        msg = (f"张数不一致：tex {len(tex_blocks)} vs md {len(tables)}。"
               f"按序号配对会改错块，请人工核对。")
        if not args.force:
            print("! " + msg + " 如确认按前 min() 张配对，请加 --force。")
            return 2
        print("! " + msg + " (--force 已指定，继续按 min() 配对)")
    count = min(len(tex_blocks), len(tables))
    for bi in range(count - 1, -1, -1):
        p = parse_html_table(tables[bi])
        if not p:
            print(f"skip table {bi}")
            continue
        rows_cells, grid, R, C = p
        rebuilt = build_longtable(rows_cells, grid, R, C, escape)
        m = tex_blocks[bi]
        tex = tex[:m.start()] + rebuilt + tex[m.end():]
        print(f"patched table {bi}: {R}x{C} (escape={escape})")
    tex_path.write_text(tex, encoding="utf-8")
    print(f"wrote {tex_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规整多卷书籍 Markdown 标题，产出适合 pandoc 转 LaTeX/PDF（干净书签）的版本。
丢弃卷首封面/版权/总目录区，正文从该卷"首章"标题开始。
标题映射：
  含"第X章"  -> 一级 `# `
  含"§"      -> 二级 `## `
  其他标题    -> 三级 `### `（文献/小标题等）
usage: python normalize_book_md.py <in.md> <first_chapter_no> <out.md>
"""
from __future__ import annotations
 
import argparse
import re
import sys
from pathlib import Path

ROMAN = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10,
         "十一":11,"十二":12,"十三":13,"十四":14,"十五":15,"十六":16,
         "十七":17,"十八":18,"十九":19,"二十":20}
CH = re.compile(r"第([一二三四五六七八九十]+)章")

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path)
    parser.add_argument("first_chapter", type=int)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    inp = args.input
    first = args.first_chapter
    outp = args.output
    lines = inp.read_text(encoding="utf-8").splitlines()
    # 找该卷首章标题所在行（含 第<first>章）
    def is_ch(ln, n):
        m = CH.search(ln)
        return bool(m) and ROMAN.get(m.group(1)) == n
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("#") and is_ch(ln, first):
            start = i
            break
    if start is None:
        # 回退：找任何含该章号的标题
        for i, ln in enumerate(lines):
            if is_ch(ln, first):
                start = i; break
    if start is None:
        print(f"ERROR: first chapter {first} not found"); return 1
    body = lines[start:]
    out = []
    for ln in body:
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if not m:
            out.append(ln); continue
        txt = m.group(2).strip()
        if not txt:
            continue
        # 按内容定级
        if is_ch(txt, first) or CH.search(txt):
            out.append("# " + txt)
        elif "§" in txt or txt.startswith("§"):
            out.append("## " + txt)
        else:
            out.append("### " + txt)
    outp.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {outp}: {len(out)} lines")
    return 0

if __name__ == "__main__":
    sys.exit(main())

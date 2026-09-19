#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repair_latex.py — 修复会导致 XeLaTeX 中断的两类结构性错误。

背景（实测结论，技能内原本无人负责）：

  1) 同一公式内出现多个 \\tag{...}
     XeLaTeX 报：  ! Package amsmath Error: Multiple \\tag.
     触发条件是「一个公式里有两个 \\tag」，与编号是否重复无关——
     已用最小用例验证：
         \\[ a=b \\tag{1}\\tag{2} \\]        -> Multiple \\tag（exit 1）
         三条公式分别 \\tag{1},\\tag{1},\\tag{1} -> 合法（exit 0）
     所以本脚本【不动编号】：跨公式重号是合法且符合原书按章编号习惯的，
     擅自全篇重编号会让正文「方程(1)」之类的引用与显示编号脱节。
     只把同一公式里多余的 \\tag 删掉，保留一个。

     --keep last  保留最后一个（默认；OCR 常把畸形 tag 吐在前面、
                  把正确 tag 吐在后面，例如 \\tag {$36^{\\prime$}}\\tag{36''}）
     --keep first 保留第一个

  2) array/tabular 行内单元格数超过列声明宽度
     XeLaTeX 报：  ! Extra alignment tab has been changed to \\cr.
     修法是按实际最大单元格数「循环补宽」列声明——无损，不删单元格、
     不改单元格内容。（原 finalize 只裁剪纯点号行，真实数据行被排除，故修不到。）

安全边界（宁可不动，也不猜）：
  - 只处理可识别的行间公式区域：\\[...\\]、$$...$$、equation/align/gather/
    multline/displaymath/eqnarray/flalign/alignat（含 * 变体）；
  - array 体内含嵌套 \\begin{...} 时跳过（行切分不可靠）；
  - 列声明含 lcr 之外的修饰（|、@、p{} 等）时跳过，不猜其语义；
  - \\tag 花括号按平衡计数解析，\\{ \\} 不计入。

用法:
  python repair_latex.py <in.tex> <out.tex> [--keep last|first] [--report r.json]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

TAG_OPEN = re.compile(r"\\tag\s*\{")
# 行间公式区域：分支1 = \[...\]，分支2 = $$...$$
DISPLAY_RE = re.compile(r"\\\[(.*?)\\\]|\$\$(.*?)\$\$", re.S)
# \begin{env}...\end{env}：env 与闭合处反向引用保持一致
ENV_RE = re.compile(
    r"\\begin\{(equation\*?|align\*?|gather\*?|multline\*?|displaymath"
    r"|eqnarray\*?|flalign\*?|alignat\*?)\}(.*?)\\end\{\1\}",
    re.S,
)
ARRAY_RE = re.compile(r"\\begin\{(array|tabular)\}\{([^}]*)\}(.*?)\\end\{\1\}", re.S)
PLAIN_SPEC = re.compile(r"^[lcr\s]*$")
COL_TYPE = re.compile(r"[lcr]")
CELL_SPLIT = re.compile(r"(?<!\\)&")


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def find_tags(text: str) -> list[tuple[int, int, int, str]]:
    """返回 [(open_start, content_start, content_end, content), ...]。

    花括号按平衡计数；反斜杠转义的花括号不计入。
    一个 tag 的完整区间是 [open_start, content_end + 1)。
    """
    out: list[tuple[int, int, int, str]] = []
    pos = 0
    while True:
        m = TAG_OPEN.search(text, pos)
        if not m:
            break
        content_start = m.end()
        depth = 1
        j = content_start
        while j < len(text):
            ch = text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:  # 花括号不闭合，放弃后续解析
            break
        out.append((m.start(), content_start, j, text[content_start:j]))
        pos = j + 1
    return out


def _regions(text: str) -> list[tuple[int, int]]:
    """所有可识别的行间公式区域 [start, end)。"""
    spans: list[tuple[int, int]] = []
    for m in DISPLAY_RE.finditer(text):
        spans.append((m.start(), m.end()))
    for m in ENV_RE.finditer(text):
        spans.append((m.start(), m.end()))
    spans.sort()
    # 去掉被完全包含的区间（例如 ENV 落在 DISPLAY 内）
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s >= merged[-1][0] and e <= merged[-1][1]:
            continue
        merged.append((s, e))
    return merged


def collapse_multiple_tags(text: str, keep: str = "last") -> tuple[str, list[str]]:
    """每个公式只保留一个 \\tag；删掉多余的。编号本身一律不改。"""
    changes: list[tuple[int, str, list[str]]] = []  # (abs_start, kept, dropped)

    for start, end in _regions(text):
        body = text[start:end]
        tags = find_tags(body)
        if len(tags) <= 1:
            continue
        kept_idx = 0 if keep == "first" else len(tags) - 1
        dropped = [c for i, (_s, _cs, _ce, c) in enumerate(tags) if i != kept_idx]
        kept = tags[kept_idx][3]
        new_body = body
        for i in sorted(range(len(tags)), reverse=True):
            if i == kept_idx:
                continue
            _s, _cs, ce, _c = tags[i]
            new_body = new_body[:_s] + new_body[ce + 1:]
        text = text[:start] + new_body + text[end:]
        changes.append((start, kept, dropped))

    return text, [
        f"L{_line_of(text, s)}: kept \\tag{{{k}}}, dropped "
        + ", ".join(f"\\tag{{{d}}}" for d in d_list)
        for s, k, d_list in changes
    ]


def _row_cell_count(row: str) -> int:
    return len(CELL_SPLIT.split(row))


def widen_arrays(text: str) -> tuple[str, list[str]]:
    """按实际最大单元格数循环补宽列声明（无损）。"""
    changes: list[str] = []

    def fix(match: re.Match[str]) -> str:
        env, spec, body = match.group(1), match.group(2), match.group(3)
        if "\\begin{" in body:  # 嵌套环境，行切分不可靠
            return match.group(0)
        if not PLAIN_SPEC.match(spec):
            return match.group(0)
        cols = COL_TYPE.findall(spec)
        n = len(cols)
        if n == 0:
            return match.group(0)
        rows = re.split(r"\\\\", body)
        widest = max((_row_cell_count(r) for r in rows), default=0)
        if widest <= n:
            return match.group(0)
        new_spec = "".join((cols * (widest // n + 1))[:widest])
        changes.append(
            f"L{_line_of(text, match.start())}: {env} cols {n} -> {widest} "
            f"({{{spec.strip()}}} -> {{{new_spec}}})"
        )
        return f"\\begin{{{env}}}{{{new_spec}}}{body}\\end{{{env}}}"

    return ARRAY_RE.sub(fix, text), changes


def repair(text: str, keep: str = "last") -> tuple[str, dict]:
    text, tag_changes = collapse_multiple_tags(text, keep)
    text, array_changes = widen_arrays(text)
    return text, {"tag_fixes": tag_changes, "array_fixes": array_changes}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--keep", choices=("last", "first"), default="last")
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args()

    original = args.input.read_text(encoding="utf-8")
    fixed, report = repair(original, args.keep)
    changed = fixed != original
    args.output.write_text(fixed, encoding="utf-8", newline="\n")

    print(f"wrote {args.output} (changed={changed})")
    for key in ("tag_fixes", "array_fixes"):
        if report[key]:
            for line in report[key]:
                print(f"- {line}")
        else:
            print(f"- {key}: none")

    if args.report:
        report.update(
            input=str(args.input),
            output=str(args.output),
            keep=args.keep,
            changed=changed,
        )
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

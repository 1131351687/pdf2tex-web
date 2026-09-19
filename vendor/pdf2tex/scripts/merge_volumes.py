#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把各块 MinerU full.md 按物理页/卷切点合并为若干卷成品 Markdown，
并把各块 images 合并进成品 images/（图片引用保持相对路径）。

用法:
  python merge_volumes.py <config.json>
config.json 示例:
{
  "out": "D:/.../成品",
  "blocks": [
    {"md": "blocks/block1-run/result/full.md", "images": "blocks/block1-run/result/images"}
  ],
  "volumes": [
    {"name": "卷Ⅰ", "file": "卷Ⅰ.md",
     "segments": [["block1", 1, null]]},            // [block标签, 起行(1-based), 止行(null=末)]
    {"name": "卷Ⅱ", "file": "卷Ⅱ.md",
     "segments": [["block2", 4014, null], ["block3",1,null], ["block4",1,null], ["block5",1,228]]},
    {"name": "卷Ⅲ", "file": "卷Ⅲ.md",
     "segments": [["block5",229,null], ["block6",1,null]]}
  ]
}
段 (block,start,end)：start/end 为 1-based 行号，含 start、不含 end；end=null 到末尾。
"""
from __future__ import annotations
 
import argparse
import json
import shutil
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    out = Path(cfg["out"])
    out.mkdir(parents=True, exist_ok=True)
    images_out = out / "images"
    images_out.mkdir(parents=True, exist_ok=True)

    # 载入各块行
    block_lines = {}
    for b in cfg["blocks"]:
        label = b["label"]
        lines = Path(b["md"]).read_text(encoding="utf-8").splitlines()
        block_lines[label] = lines
        print(f"loaded {label}: {len(lines)} lines")

    # 复制图片（全局 hash 唯一，合并无冲突）
    copied = 0
    for b in cfg["blocks"]:
        imgdir = Path(b["images"])
        if not imgdir.is_dir():
            continue
        for f in imgdir.iterdir():
            if f.is_file():
                dest = images_out / f.name
                if not dest.exists():
                    shutil.copy2(f, dest)
                copied += 1
    print(f"images merged: {copied}")

    # 组装各卷
    for vol in cfg["volumes"]:
        parts = []
        for seg in vol["segments"]:
            label, start, end = seg[0], seg[1], seg[2]
            lines = block_lines[label]
            s = start - 1
            e = len(lines) if end is None else end - 1
            parts.extend(lines[s:e])
            # 卷边界加分隔
            parts.append("")
        body = "\n".join(parts).strip() + "\n"
        (out / vol["file"]).write_text(body, encoding="utf-8")
        print(f"wrote {vol['file']}: {len(parts)} lines, {len(body)} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

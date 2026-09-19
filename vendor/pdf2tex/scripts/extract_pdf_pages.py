#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从源 PDF 抽出指定物理页，生成只含这些页的子 PDF。

为什么要这个脚本：MinerU 的 Agent 端点上限定为 20 页；而修缺页/补段落时
通常只需要重跑 3~5 页。子 PDF 让重跑更快、更省额度，并且把"物理页 ->
子 PDF 页"的对应关系写进 manifest，避免把补回的文字拼到错误的位置。

页面编号一律用 1 起的物理页号（与 PDF 阅读器显示的顺序一致），
与"印刷页码"无关——扫描件的两者往往有固定偏移，偏移请用
audit_scan_pages.py 的 --anchor 推导。

用法：
  python extract_pdf_pages.py in.pdf out.pdf --pages 935-939
  python extract_pdf_pages.py in.pdf out.pdf --pages 935,937-939 --manifest pages.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def parse_pages(spec: str) -> list[int]:
    """把 "935-939,941" 解析成 [935,936,937,938,939,941]。"""
    pages: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo_s, _, hi_s = chunk.partition("-")
            lo, hi = int(lo_s), int(hi_s)
            if hi < lo:
                raise ValueError(f"倒序区间: {chunk}")
            pages.extend(range(lo, hi + 1))
        else:
            pages.append(int(chunk))
    if not pages:
        raise ValueError(f"空的页范围: {spec!r}")
    if len(set(pages)) != len(pages):
        raise ValueError(f"页号重复: {spec!r}")
    return pages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pages", required=True,
                        help='物理页号，1 起，如 "935-939" 或 "935,937-939"')
    parser.add_argument("--manifest", type=Path,
                        help="可选的 JSON 清单：记录 子 PDF 页 -> 源物理页")
    parser.add_argument("--overwrite", action="store_true",
                        help="输出已存在时覆盖")
    args = parser.parse_args()

    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:  # 退回到旧包名
        from PyPDF2 import PdfReader, PdfWriter  # type: ignore

    src = args.pdf.resolve()
    if not src.is_file():
        parser.error(f"找不到源 PDF: {src}")
    if args.output.exists() and not args.overwrite:
        parser.error(f"输出已存在（加 --overwrite）: {args.output}")

    pages = parse_pages(args.pages)
    reader = PdfReader(str(src))
    total = len(reader.pages)
    out_of_range = [p for p in pages if p < 1 or p > total]
    if out_of_range:
        parser.error(f"页号越界 (源共 {total} 页): {out_of_range}")

    writer = PdfWriter()
    for page_no in pages:
        writer.add_page(reader.pages[page_no - 1])
    writer.add_metadata({
        "/Title": f"{src.stem} pages {args.pages}",
        "/Producer": "pdf2tex extract_pdf_pages.py",
        "/Subject": f"subset of {src.name}: {args.pages}",
    })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as stream:
        writer.write(stream)

    subset_pages = [
        {"subset_page": index + 1, "source_page": page_no}
        for index, page_no in enumerate(pages)
    ]
    manifest = {
        "source_pdf": str(src),
        "source_page_count": total,
        "page_spec": args.pages,
        "source_pages": pages,
        "subset_pdf": str(args.output.resolve()),
        "subset_page_count": len(pages),
        "subset_pages": subset_pages,
        "created_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(f"源: {src.name}  ({total} 页)")
    print(f"子: {args.output.name}  ({len(pages)} 页)  "
          f"{args.output.stat().st_size / 1024:.0f} KB")
    print("物理页映射 (子页 -> 源物理页):")
    for item in subset_pages:
        print(f"  子 {item['subset_page']:>3}  ->  源 {item['source_page']}")
    if args.manifest:
        print(f"清单: {args.manifest}")
    print("提示: 交 OCR 时用子 PDF；回填正文必须按上面的映射，别用子 PDF 页号。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
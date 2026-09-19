#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计图片型扫描 PDF 的页序完整性：重复页、疑似缺页、物理页→印刷页码映射。

典型问题：扫描件在某个物理页区间重复扫入同一印刷页，导致另一印刷页根本没
进扫描件；此时纯按“物理页 − 常数偏移”推导页码会错位。该脚本把这种页序
异常自动查出来，而不是靠肉眼翻页。

原理（不需要 OCR）：
  1. 低分辨率渲染每页灰度图；
  2. 取页眉/页脚带（页码通常在那里）做指纹，按“几乎相同即同一页码”聚类；
  3. 两张不相邻的页落在同一个页码簇 ⇒ 其中必有一页不符合恒定偏移，
     即扫描件重复/错页，据此反推缺页位置；
  4. 有文本层的 PDF 直接读页脚数字，得到真实映射（免费且准确）。
     传 --anchor 给出已知的“物理页=印刷页”对时输出外推映射，
     外推值一律标注 derived，与真实读到的值区分开。

用法：
  python audit_scan_pages.py scan.pdf --start 925 --end 945 --anchor 930=188,940=198
  python audit_scan_pages.py scan.pdf --start 925 --end 945 --out audit.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

FOOTER_DEFAULT = "0.90,0.99"   # 页脚带（页高比例），页码多在此
HEADER_DEFAULT = "0.02,0.10"   # 页眉带


def parse_anchors(spec: str | None) -> list[tuple[int, int]]:
    """解析 "930=188,940=198" → [(930,188),(940,198)]。"""
    if not spec:
        return []
    anchors: list[tuple[int, int]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        left, _, right = chunk.partition("=")
        anchors.append((int(left), int(right)))
    anchors.sort()
    return anchors


def parse_band(value: str) -> tuple[float, float] | None:
    if value.strip().lower() in ("none", "no", "-"):
        return None
    lo, _, hi = value.partition(",")
    return (float(lo), float(hi))


def render_gray(doc, page_no: int, zoom: float) -> np.ndarray:
    import fitz  # PyMuPDF

    page = doc.load_page(page_no - 1)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
    array = np.frombuffer(pix.samples, dtype=np.uint8)
    return array.reshape(pix.height, pix.width).copy()


def band(array: np.ndarray, band_spec: tuple[float, float]) -> np.ndarray:
    height = array.shape[0]
    top = max(0, min(height - 1, int(round(band_spec[0] * height))))
    bottom = max(top + 1, min(height, int(round(band_spec[1] * height))))
    return array[top:bottom]


def fingerprint(array: np.ndarray, size: int = 48) -> np.ndarray:
    """把一块图像重采样成 size×size 灰度矩阵，用于“是否同一页”的比较。"""
    height, width = array.shape
    if height < 2 or width < 2:
        return np.zeros((size, size), dtype=np.float32)
    row_idx = np.linspace(0, height - 1, size).astype(int)
    col_idx = np.linspace(0, width - 1, size).astype(int)
    return array[np.ix_(row_idx, col_idx)].astype(np.float32)


def distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(np.abs(a - b)))


def read_footer_numbers(doc, page_no: int) -> list[int]:
    """有文本层时：从独立的短行里抠出页码数字。"""
    page = doc.load_page(page_no - 1)
    text = page.get_text("text") or ""
    numbers: list[int] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or len(line) > 12:
            continue
        match = re.fullmatch(r"[\[\(（]?(\d{1,4})[\]\)）]?", line)
        if match:
            numbers.append(int(match.group(1)))
    return numbers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--start", type=int, default=1, help="起始物理页(含)")
    parser.add_argument("--end", type=int, default=None, help="终止物理页(含)")
    parser.add_argument("--zoom", type=float, default=0.6,
                        help="渲染缩放，默认 0.6（看清带区结构即可）")
    parser.add_argument("--footer", default=FOOTER_DEFAULT,
                        help="页脚带比例，如 0.90,0.99；传 none 表示不看页脚")
    parser.add_argument("--header", default=HEADER_DEFAULT,
                        help="页眉带比例；传 none 表示不看页眉")
    parser.add_argument("--threshold", type=float, default=15.0,
                        help="页眉/页脚带聚类的平均灰度差阈值，默认 15.0")
    parser.add_argument("--duplicate-threshold", type=float, default=20.0,
                        help="整页近重复阈值（0 表示关闭），默认 20.0")
    parser.add_argument("--anchor", default=None,
                        help="已知映射对，如 930=188,940=198")
    parser.add_argument("--out", type=Path, help="输出 JSON 报告")
    parser.add_argument("--quiet", action="store_true",
                        help="只写 JSON 报告，不输出逐页表和完整重复页清单")
    args = parser.parse_args()

    try:
        import fitz
    except ImportError:
        print("需要 PyMuPDF(fitz)：本机 D:\\miniconda\\python.exe 里已具备",
              file=sys.stderr)
        return 2

    src = args.pdf.resolve()
    if not src.is_file():
        parser.error(f"找不到 PDF: {src}")
    doc = fitz.open(src)
    total = doc.page_count
    start = max(1, args.start)
    end = min(total, args.end or total)
    if end < start:
        parser.error(f"页范围为空: {start}~{end}（源共 {total} 页）")
    window = list(range(start, end + 1))

    footer_band = parse_band(args.footer)
    header_band = parse_band(args.header)

    full_fp: dict[int, np.ndarray] = {}
    band_fp: dict[int, list[np.ndarray]] = {}
    read_numbers: dict[int, list[int]] = {}
    has_text = False

    print(f"渲染 {start}~{end}（共 {len(window)} 页，zoom={args.zoom}）...")
    for page_no in window:
        gray = render_gray(doc, page_no, args.zoom)
        full_fp[page_no] = fingerprint(gray, 40)
        parts: list[np.ndarray] = []
        if header_band:
            parts.append(fingerprint(band(gray, header_band), 48))
        if footer_band:
            parts.append(fingerprint(band(gray, footer_band), 48))
        band_fp[page_no] = parts
        numbers = read_footer_numbers(doc, page_no)
        if numbers:
            has_text = True
            read_numbers[page_no] = numbers

    # --- 1) 整页近似重复 ---
    duplicates: list[dict[str, object]] = []
    for index, page_a in enumerate(window):
        for page_b in window[index + 1:]:
            delta = distance(full_fp[page_a], full_fp[page_b])
            if args.duplicate_threshold > 0 and delta <= args.duplicate_threshold:
                duplicates.append({
                    "pages": [page_a, page_b],
                    "distance": round(delta, 3),
                    "adjacent": page_b - page_a == 1,
                })

    # --- 2) 页眉+页脚带的页码聚类 ---
    clusters: dict[int, int] = {}
    representatives: list[list[np.ndarray]] = []
    for page_no in window:
        parts = band_fp[page_no]
        if not parts or not all(part.any() for part in parts):
            clusters[page_no] = -1
            continue
        best_id: int | None = None
        best_delta: float | None = None
        for cid, rep in enumerate(representatives):
            delta = max(distance(part, ref) for part, ref in zip(parts, rep))
            if best_delta is None or delta < best_delta:
                best_id, best_delta = cid, delta
        if best_id is not None and best_delta is not None and best_delta <= args.threshold:
            clusters[page_no] = best_id
        else:
            representatives.append(parts)
            clusters[page_no] = len(representatives) - 1

    cluster_members: dict[int, list[int]] = {}
    for page_no, cid in clusters.items():
        if cid >= 0:
            cluster_members.setdefault(cid, []).append(page_no)
    # --- 3) 同一页码簇出现在不相邻的页上 ⇒ 重复页/错页 ---
    repeated_clusters = {
        cid: members for cid, members in cluster_members.items()
        if len(members) > 1 and any(b - a > 1 for a, b in zip(members, members[1:]))
    }
    suspicious = sorted({p for members in repeated_clusters.values() for p in members})

    # --- 4) 映射：文本层直接读；否则用锚点外推（明确标注 derived） ---
    anchors = parse_anchors(args.anchor)
    derived: dict[int, str] = {}
    anchor_issues: list[str] = []
    for page_no, _printed in anchors:
        if page_no not in clusters:
            anchor_issues.append(f"锚点页 {page_no} 不在审计区间内")
        elif page_no in suspicious:
            anchor_issues.append(f"锚点页 {page_no} 落在重复页上，作锚点不可靠")
    for index, (page_no, printed) in enumerate(anchors):
        next_page = anchors[index + 1][0] if index + 1 < len(anchors) else None
        last = min((next_page - 1) if next_page else window[-1], window[-1])
        for offset, current in enumerate(range(page_no, last + 1)):
            value = str(printed + offset)
            if current in suspicious:
                value += "?"
            derived[current] = value

    report = {
        "schema": 1,
        "pdf": str(src),
        "page_count": total,
        "window": [start, end],
        "band_threshold": args.threshold,
        "duplicate_threshold": args.duplicate_threshold,
        "has_text_layer": has_text,
        "duplicate_pages": duplicates,
        "page_number_clusters": {str(p): c for p, c in sorted(clusters.items())},
        "repeated_number_clusters": {
            str(cid): members for cid, members in sorted(repeated_clusters.items())
        },
        "suspicious_pages": suspicious,
        "read_numbers": {str(p): v for p, v in sorted(read_numbers.items())},
        "anchors": [{"page": p, "printed": n} for p, n in anchors],
        "derived_map": {str(p): v for p, v in sorted(derived.items())},
        "anchor_issues": anchor_issues,
    }

    if args.quiet:
        print(f"audit: {len(window)} pages, has_text={has_text}, "
              f"duplicate_pages={len(duplicates)}, "
              f"suspicious_pages={len(suspicious)}")
    else:
        print()
        print("物理页 | 页码簇 | 读到的页码 | 推导印刷页")
        for page_no in window:
            mark = "  <== 可疑" if page_no in suspicious else ""
            numbers = ",".join(str(n) for n in read_numbers.get(page_no, [])) or "-"
            print(f"{page_no:>6} | {clusters[page_no]:>6} | {numbers:>10} | "
                  f"{derived.get(page_no, '-'):>10}{mark}")

        print()
        if duplicates:
            print(f"整页近似重复 {len(duplicates)} 对：")
            for item in duplicates:
                pages = item["pages"]
                kind = "相邻" if item["adjacent"] else "不相邻"
                print(f"  物理 {pages[0]} ≡ {pages[1]}（{kind}，距离 {item['distance']}）")
        else:
            print("未发现整页近似重复。")

        if repeated_clusters:
            print("页眉/页脚指纹相同的页组（同一印刷页码被扫了多次）：")
            for cid, members in sorted(repeated_clusters.items()):
                print(f"  簇 {cid}: 物理页 {members}")
            print("  => 这些页里至少有一页不符合恒定偏移：扫描件在此重复，"
                  "通常意味着有一个印刷页整体缺失。")
        else:
            print("页眉/页脚指纹没有跨页重合，未发现重复页码。")

        if anchor_issues:
            print("锚点问题：")
            for issue in anchor_issues:
                print("  - " + issue)

        if not has_text:
            print("该 PDF 无文本层（图片型扫描件），页码无法直接读取；"
                  "上面的“推导印刷页”仅由 --anchor 外推，带 ? 的落在重复页上、不可信。")
        print("注意：确认缺页后，不要凭上下文补写正文；要么换一个可靠底本，"
              "要么在正文中显式标注缺页（见 SKILL.md 的缺页标注约定）。")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"报告: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

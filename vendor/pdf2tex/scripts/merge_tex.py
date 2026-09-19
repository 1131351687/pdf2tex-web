#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Merge native MinerU TeX bodies into configured, compilable TeX volumes.

MinerU chapter headings are expected in the form
``\subsection{第X章 ...}\label{...}``. The JSON config assigns inclusive
chapter ranges to output files and supplies one block per MinerU result:

{
  "out": "D:/ocr/book-tex",
  "blocks": [
    {"label": "block1", "tex": "D:/ocr/block1/result/full.fixed.tex"},
    {"label": "block2", "tex": "D:/ocr/block2/result/full.fixed.tex"}
  ],
  "volumes": [
    {"name": "Volume I", "file": "volume-1.tex", "chapter_ranges": [[1, 4]]},
    {"name": "Volume II", "file": "volume-2.tex", "chapter_ranges": [[5, 8]]},
    {"name": "Volume III", "file": "volume-3.tex", "chapter_ranges": [[9, 12]]}
  ]
}

The first block's preamble is reused for every output volume. Chapter ranges
must not overlap between volumes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
CHAPTER_RE = re.compile(r"第\s*([0-9零一二三四五六七八九十百两]+)\s*章")


def parse_chapter_number(value: str) -> int:
    if value.isdigit():
        return int(value)

    total = 0
    pending = 0
    for char in value:
        if char in DIGITS:
            pending = DIGITS[char]
        elif char == "十":
            total += (pending or 1) * 10
            pending = 0
        elif char == "百":
            total += (pending or 1) * 100
            pending = 0
        else:
            raise ValueError(f"unsupported chapter number: {value}")
    return total + pending


def chapter_number(line: str) -> int | None:
    match = CHAPTER_RE.search(line)
    if not match:
        return None
    return parse_chapter_number(match.group(1))


def split_into_chapters(lines: list[str]) -> list[tuple[int, int, int]]:
    """Return ``(chapter_number, start, end)`` segments for chapter headings."""
    starts: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if "\\subsection{" not in line:
            continue
        number = chapter_number(line)
        if number is not None:
            starts.append((index, number))

    if not starts:
        raise ValueError("no chapter subsections matching '第X章' were found")

    result: list[tuple[int, int, int]] = []
    for index, (start, number) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
        result.append((number, start, end))
    return result


def load_volumes(config: dict) -> list[dict]:
    raw_volumes = config.get("volumes")
    if not isinstance(raw_volumes, list) or not raw_volumes:
        raise ValueError("config must define a non-empty 'volumes' list")

    volumes: list[dict] = []
    owners: dict[int, str] = {}
    for raw in raw_volumes:
        if not isinstance(raw, dict):
            raise ValueError("each volume entry must be an object")
        name = str(raw.get("name") or raw.get("file") or "").strip()
        filename = str(raw.get("file") or "").strip()
        ranges = raw.get("chapter_ranges")
        if not name or not filename:
            raise ValueError("each volume needs a non-empty 'name' or 'file'")
        if Path(filename).name != filename:
            raise ValueError(f"volume file must be a bare filename: {filename}")
        if not isinstance(ranges, list) or not ranges:
            raise ValueError(f"{name}: 'chapter_ranges' must be a non-empty list")

        normalized_ranges: list[tuple[int, int]] = []
        for item in ranges:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not all(isinstance(value, int) for value in item)
            ):
                raise ValueError(f"{name}: each chapter range must be [start, end]")
            start, end = item
            if start < 1 or end < start:
                raise ValueError(f"{name}: invalid chapter range [{start}, {end}]")
            for chapter in range(start, end + 1):
                previous = owners.get(chapter)
                if previous is not None:
                    raise ValueError(
                        f"chapter {chapter} is assigned to both {previous} and {name}"
                    )
                owners[chapter] = name
            normalized_ranges.append((start, end))

        volumes.append(
            {"name": name, "file": filename, "chapter_ranges": normalized_ranges}
        )
    return volumes


def belongs_to(number: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= number <= end for start, end in ranges)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", type=Path)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    volumes = load_volumes(config)
    blocks = config.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("config must define a non-empty 'blocks' list")

    out_dir = Path(config["out"])
    out_dir.mkdir(parents=True, exist_ok=True)
    collected: dict[str, list[tuple[str, int, list[str]]]] = {
        volume["name"]: [] for volume in volumes
    }
    master_preamble: str | None = None
    chapter_occurrences: dict[int, list[str]] = {}

    for block in blocks:
        label = str(block.get("label") or Path(block["tex"]).stem)
        tex_path = Path(block["tex"])
        tex = tex_path.read_text(encoding="utf-8")
        begin = tex.find("\\begin{document}")
        end = tex.find("\\end{document}", begin + 1)
        if begin < 0 or end < 0 or end <= begin:
            raise ValueError(f"{label}: missing or malformed document environment")

        preamble = tex[:begin]
        body = tex[begin + len("\\begin{document}") : end]
        if master_preamble is None:
            master_preamble = preamble

        chapters = split_into_chapters(body.splitlines())
        print(f"{label}: found {len(chapters)} chapter subsection(s)")
        for number, start, end_line in chapters:
            chapter_occurrences.setdefault(number, []).append(label)
            lines = body.splitlines()[start:end_line]
            matched = [
                volume
                for volume in volumes
                if belongs_to(number, volume["chapter_ranges"])
            ]
            if not matched:
                print(f"  warning: chapter {number} is not assigned to a volume")
                continue
            volume = matched[0]
            collected[volume["name"]].append((label, number, lines))

    if master_preamble is None:
        raise ValueError("no preamble was found in any input block")

    for number, labels in sorted(chapter_occurrences.items()):
        if len(labels) > 1:
            print(
                f"warning: chapter {number} occurs in multiple blocks "
                f"({', '.join(labels)}); all occurrences are preserved"
            )

    for volume in volumes:
        entries = collected[volume["name"]]
        if not entries:
            print(f"warning: {volume['name']} has no matching chapters")
            continue
        for _, number, _ in entries:
            if number not in chapter_occurrences:
                raise AssertionError("internal chapter allocation error")
        body_parts = ["\n".join(lines).strip("\n") for _, _, lines in entries]
        body = "\n\n".join(part for part in body_parts if part).strip() + "\n"
        document = (
            master_preamble.rstrip()
            + "\n\n\\begin{document}\n"
            + body
            + "\n\\end{document}\n"
        )
        target = out_dir / volume["file"]
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(document, encoding="utf-8")
        temporary.replace(target)
        print(
            f"wrote {target}: {len(body)} chars, {len(entries)} chapter subsection(s)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

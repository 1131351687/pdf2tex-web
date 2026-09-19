#!/usr/bin/env python3
"""Fast structural QA for merged Markdown before LaTeX conversion."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HTML_IMAGE_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)


def strip_code(markdown: str) -> str:
    return re.sub(r"```.*?```", "", markdown, flags=re.S)


def image_report(markdown: str, base_dir: Path) -> dict[str, object]:
    refs = IMAGE_RE.findall(markdown) + HTML_IMAGE_RE.findall(markdown)
    missing: list[str] = []
    for ref in refs:
        path = base_dir / ref.split("#", 1)[0].split("?", 1)[0]
        if not path.is_file():
            missing.append(ref)
    return {
        "image_refs": len(refs),
        "missing_image_refs": missing[:100],
        "missing_image_count": len(missing),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("markdown", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    markdown = args.markdown.read_text(encoding="utf-8", errors="replace")
    clean = strip_code(markdown)
    replacements = markdown.count("\ufffd")
    math_block_count = clean.count("$$") // 2
    unbalanced_math_blocks = clean.count("$$") % 2
    inline_dollar_pairs = 0
    inline_unbalanced = 0
    outside_blocks = re.sub(r"\$\$.*?\$\$", "", clean, flags=re.S)
    dollars = re.findall(r"(?<!\\)\$", outside_blocks)
    inline_dollar_pairs = len(dollars) // 2
    inline_unbalanced = len(dollars) % 2

    report = {
        "markdown": str(args.markdown.resolve()),
        "chars": len(markdown),
        "lines": markdown.count("\n") + 1,
        "replacement_chars": replacements,
        "math_block_count": math_block_count,
        "unbalanced_math_blocks": unbalanced_math_blocks,
        "inline_dollar_pairs": inline_dollar_pairs,
        "unbalanced_inline_math": inline_unbalanced,
        **image_report(markdown, args.markdown.parent),
    }
    passed = (
        replacements == 0
        and unbalanced_math_blocks == 0
        and inline_unbalanced == 0
        and report["missing_image_count"] == 0
    )
    report["passed"] = passed
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

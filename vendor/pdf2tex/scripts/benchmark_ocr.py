#!/usr/bin/env python3
"""Compare OCR Markdown outputs using transparent, reproducible heuristics."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from pathlib import Path


WORD = re.compile(r"[A-Za-z]{2,}")
MATH = re.compile(r"(?<!\\)\$\$.*?(?<!\\)\$\$|(?<!\\)\$(?!\$).*?(?<!\\)\$", re.S)
LATEX_COMMAND = re.compile(r"\\[A-Za-z]+")
MOJIBAKE = re.compile(r"[\uFFFD]|(?:Ã.|Â.|â€|鈫|锛|銆)")
SEVERE_TEX_ARTIFACT = re.compile(
    r"\\delimiterspace|\\downharpoonright|\\not\s+V|"
    r"\\kern\s+-?\s*delimiterspace"
)


def metrics(path: Path, reference_words: set[str]) -> dict[str, object]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    words = [word.lower() for word in WORD.findall(text)]
    word_set = set(words)
    math = MATH.findall(text)
    dollars = len(re.findall(r"(?<!\\)\$", text))
    command_counts = Counter(LATEX_COMMAND.findall("\n".join(math)))
    nonempty_lengths = [len(line) for line in lines if line.strip()]
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "characters": len(text),
        "lines": len(lines),
        "nonempty_lines": sum(bool(line.strip()) for line in lines),
        "median_nonempty_line_length": round(statistics.median(nonempty_lengths), 2) if nonempty_lengths else 0,
        "headings": sum(line.lstrip().startswith("#") for line in lines),
        "images": len(re.findall(r"!\[[^]]*\]\([^)]+\)", text)),
        "tables": sum(line.count("|") >= 2 for line in lines),
        "math_spans": len(math),
        "latex_commands_in_math": sum(command_counts.values()),
        "unbalanced_dollar": bool(dollars % 2),
        "replacement_or_mojibake": len(MOJIBAKE.findall(text)),
        "severe_tex_artifacts": len(SEVERE_TEX_ARTIFACT.findall(text)),
        "unique_words": len(word_set),
        "reference_word_recall": round(len(word_set & reference_words) / max(1, len(reference_words)), 4),
        "top_math_commands": command_counts.most_common(12),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("markdown", nargs="+", type=Path)
    args = parser.parse_args()
    reference = args.reference.read_text(encoding="utf-8", errors="replace")
    reference_words = {word.lower() for word in WORD.findall(reference)}
    report = {
        "method": "Heuristic structural comparison; pdftotext word set is a noisy reference, not ground truth.",
        "reference_unique_words": len(reference_words),
        "results": [metrics(path, reference_words) for path in args.markdown],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Chunked LLM proofreading for OCR-generated Markdown."""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.llm import LLMClient, LLMError


@dataclass(frozen=True)
class Chunk:
    index: int
    start: int
    end: int
    text: str


@dataclass
class ProofreadOutcome:
    markdown: str
    total_chunks: int
    applied: int
    skipped: int
    warnings: list[str]


_HEADING_RE = re.compile(r"^#{1,6}(?:\s|$)")
_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*(?:<([^<>]*)>|([^)\s]+))[^)]*\)"
)


def _image_paths(text: str) -> set[str]:
    paths: set[str] = set()
    for match in _IMAGE_RE.finditer(text):
        path = match.group(1) if match.group(1) is not None else match.group(2)
        if path is not None:
            paths.add(path)
    return paths


def _add_lines_as_atoms(
    lines: list[str], starts: list[int], first: int, last: int, atoms: list[tuple[int, int, str]]
) -> None:
    for line_no in range(first, last):
        atoms.append((starts[line_no], starts[line_no] + len(lines[line_no]), "normal"))


def _markdown_atoms(text: str, max_chars: int) -> list[tuple[int, int, str]]:
    lines = text.splitlines(keepends=True)
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)

    atoms: list[tuple[int, int, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            atoms.append((starts[index], starts[index] + len(line), "blank"))
            index += 1
            continue

        if _HEADING_RE.match(stripped):
            atoms.append((starts[index], starts[index] + len(line), "heading"))
            index += 1
            continue

        if stripped.startswith("```"):
            end_index = index + 1
            while end_index < len(lines) and not lines[end_index].strip().startswith("```"):
                end_index += 1
            if end_index < len(lines):
                end_index += 1
            start = starts[index]
            end = starts[end_index] if end_index < len(lines) else len(text)
            atoms.append((start, end, "fence"))
            if end - start > max_chars:
                warnings.warn(f"Markdown 代码围栏超过 max_chars（{end - start} 字符）")
            index = end_index
            continue

        if stripped.startswith("$$"):
            if stripped.count("$$") >= 2:
                end_index = index + 1
            else:
                end_index = index + 1
                while end_index < len(lines) and "$$" not in lines[end_index]:
                    end_index += 1
                if end_index < len(lines):
                    end_index += 1
            start = starts[index]
            end = starts[end_index] if end_index < len(lines) else len(text)
            atoms.append((start, end, "math"))
            if end - start > max_chars:
                warnings.warn(f"Markdown 公式块超过 max_chars（{end - start} 字符）")
            index = end_index
            continue

        paragraph_end = index + 1
        while paragraph_end < len(lines):
            candidate = lines[paragraph_end].strip()
            if (
                not candidate
                or _HEADING_RE.match(candidate)
                or candidate.startswith("```")
                or candidate.startswith("$$")
            ):
                break
            paragraph_end += 1
        start = starts[index]
        end = starts[paragraph_end] if paragraph_end < len(lines) else len(text)
        if end - start <= max_chars:
            atoms.append((start, end, "normal"))
        else:
            _add_lines_as_atoms(lines, starts, index, paragraph_end, atoms)
        index = paragraph_end

    return atoms


def split_markdown(
    text: str, *, max_chars: int = 6000, max_chunks: int = 200
) -> list[Chunk]:
    """Split Markdown at structural boundaries without cutting atomic blocks."""
    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0")
    if max_chunks < 0:
        raise ValueError("max_chunks 不能为负")

    raw_atoms = _markdown_atoms(text, max_chars)
    ranges: list[tuple[int, int, str]] = []
    current: tuple[int, int, str] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            ranges.append(current)
            current = None

    for start, end, kind in raw_atoms:
        if kind == "blank":
            if current is None:
                current = (start, end, kind)
            else:
                current = (current[0], end, current[2])
            flush()
            continue

        if kind == "heading":
            flush()
            current = (start, end, kind)
            continue

        if current is None:
            current = (start, end, kind)
        elif end - current[0] <= max_chars:
            current = (current[0], end, current[2])
        else:
            flush()
            current = (start, end, kind)
    flush()

    if len(ranges) > max_chunks:
        raise ValueError("文档太大或建议关闭校对")

    return [
        Chunk(index=index, start=start, end=end, text=text[start:end])
        for index, (start, end, _kind) in enumerate(ranges)
    ]


def _is_whole_code_shell(candidate: str) -> bool:
    stripped = candidate.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return False
    lines = stripped.splitlines()
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return False
    opener = lines[0].strip()
    language = opener[3:].strip().lower()
    return language in {"", "markdown", "md", "text"}


def _valid_revision(original: str, candidate: str) -> bool:
    if not candidate.strip():
        return False
    if not 0.5 <= len(candidate) / len(original) <= 1.5:
        return False
    if candidate.count("$$") % 2 != original.count("$$") % 2:
        return False
    if _image_paths(candidate) != _image_paths(original):
        return False
    if "\ufffd" in candidate:
        return False
    return not _is_whole_code_shell(candidate)


def proofread_markdown(
    text: str,
    client: LLMClient,
    *,
    max_chars: int = 6000,
    max_chunks: int = 200,
    log: Callable[[str], None] | None = None,
    progress: Callable[[float], None] | None = None,
) -> ProofreadOutcome:
    chunks = split_markdown(text, max_chars=max_chars, max_chunks=max_chunks)
    warnings_out: list[str] = []
    applied = 0
    skipped = 0
    pieces: list[str] = []

    def emit(message: str) -> None:
        warnings_out.append(message)
        if log is not None:
            log(message)

    for index, chunk in enumerate(chunks):
        revised: str | None = None
        try:
            raw = client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是严谨的 OCR Markdown 校对器。只修 OCR 错误：错别字、"
                            "公式符号、表格错位。保留 Markdown 结构、图片引用和数学定界符。"
                            "不要解释，只输出修订后的 Markdown。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "请校对下面 Markdown 片段，只修 OCR 错误，保留全部 Markdown 结构、"
                            "图片引用和数学定界符，只输出修订后的 Markdown：\n"
                            "```markdown\n"
                            f"{chunk.text}\n"
                            "```"
                        ),
                    },
                ],
                temperature=0.0,
            )
            if isinstance(raw, str) and _valid_revision(chunk.text, raw):
                revised = raw
            else:
                emit(f"校对块 {index + 1} 未通过安全校验，已保留原文")
        except LLMError as exc:
            emit(f"校对块 {index + 1} 失败，已保留原文: {exc}")

        if revised is None:
            skipped += 1
            pieces.append(chunk.text)
        else:
            applied += 1
            pieces.append(revised)
        if progress is not None:
            progress((index + 1) / len(chunks) if chunks else 1.0)

    if progress is not None and not chunks:
        progress(1.0)

    return ProofreadOutcome(
        markdown="".join(pieces),
        total_chunks=len(chunks),
        applied=applied,
        skipped=skipped,
        warnings=warnings_out,
    )

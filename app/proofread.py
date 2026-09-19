"""Chunked LLM proofreading for OCR-generated Markdown."""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    max_workers: int = 1,
    max_consecutive_failures: int | None = None,
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

    workers = max(1, int(max_workers))
    limit = max_consecutive_failures
    if limit is not None:
        limit = max(1, int(limit))

    def one(chunk: Chunk) -> str | None:
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
                return raw
            emit(f"校对块 {chunk.index + 1} 未通过安全校验，已保留原文")
        except LLMError as exc:
            emit(f"校对块 {chunk.index + 1} 失败，已保留原文: {exc}")
        return None

    if workers == 1 or len(chunks) <= 1:
        consecutive_failures = 0
        for chunk in chunks:
            revised = one(chunk)
            if revised is None:
                skipped += 1
                pieces.append(chunk.text)
                consecutive_failures += 1
                if limit is not None and consecutive_failures >= limit:
                    emit(
                        "连续校对失败已达到上限，剩余块保留原文以避免浪费时间。"
                    )
                    remaining = chunks[len(pieces):]
                    skipped += len(remaining)
                    pieces.extend(chunk.text for chunk in remaining)
                    if progress is not None:
                        progress(1.0)
                    break
            else:
                applied += 1
                pieces.append(revised)
                consecutive_failures = 0
            if progress is not None and len(pieces) <= len(chunks):
                progress(min(1.0, len(pieces) / len(chunks)))
    else:
        # 并发模式让已提交任务全部完成，避免取消语义引入部分结果丢失；
        # 连续失败计数在这里仅作为 summary 警示，不截断全文。
        results: dict[int, str] = {}
        failures: list[int] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(one, chunk): chunk for chunk in chunks}
            done_count = 0
            for future in as_completed(futures):
                chunk = futures[future]
                revised = future.result()
                done_count += 1
                if revised is None:
                    failures.append(chunk.index)
                else:
                    results[chunk.index] = revised
                if progress is not None:
                    progress(min(1.0, done_count / len(chunks)))

        applied = len(results)
        skipped = len(chunks) - applied
        pieces = [results.get(chunk.index, chunk.text) for chunk in chunks]

        # 找出最长连续失败游程，用于提示模型/参数可能不适合这本书。
        longest_run = 0
        current_run = 0
        failed_set = set(failures)
        for chunk in chunks:
            if chunk.index in failed_set:
                current_run += 1
                longest_run = max(longest_run, current_run)
            else:
                current_run = 0
        if limit is not None and longest_run >= limit:
            emit(
                f"并发校对最长连续失败 {longest_run} 块，"
                "建议检查模型能力或关闭校对。"
            )

    return ProofreadOutcome(
        markdown="".join(pieces),
        total_chunks=len(chunks),
        applied=applied,
        skipped=skipped,
        warnings=warnings_out,
    )

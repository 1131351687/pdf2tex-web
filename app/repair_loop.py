"""XeLaTeX compilation and conservative LLM-assisted repair."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.llm import LLMClient, LLMError


@dataclass
class CompileOutcome:
    passed: bool
    rounds_used: int
    report: dict[str, Any]
    tex: Path
    log_path: Path
    warnings: list[str] = field(default_factory=list)


_MAX_CONTEXT_LINES = 120
_LINE_RE = re.compile(r"(?:^|\s)l\.(\d+)", re.M)
_ERROR_LINE_RE = re.compile(r"^!\s*(.+)$", re.M)


def _failed_report(tex: Path, log_path: Path, message: str) -> dict[str, Any]:
    return {
        "passed": False,
        "returncode": -1,
        "timed_out": False,
        "error_count": 1,
        "errors": [message],
        "missing_glyph_count": 0,
    }


def compile_tex(tex: Path, work_dir: Path) -> tuple[dict[str, Any], Path]:
    """Run the vendor validator and return its JSON report and log path."""
    tex = Path(tex)
    work_dir = Path(work_dir)
    script = Path(__file__).resolve().parents[1] / "vendor" / "pdf2tex" / "scripts" / "validate_latex.py"
    report_path = work_dir / "compile-report.json"
    log_path = work_dir / f"{tex.stem}.log"
    work_dir.mkdir(parents=True, exist_ok=True)

    if not tex.is_file():
        raise RuntimeError(f"TeX 文件不存在: {tex}")
    if not script.is_file():
        raise RuntimeError(f"编译校验脚本不存在: {script}")

    try:
        completed = subprocess.run(
            [sys.executable, str(script), str(tex), str(work_dir)],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=620,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("LaTeX 校验超时（620 秒）") from exc
    except OSError as exc:
        raise RuntimeError(f"无法启动 LaTeX 校验脚本: {exc}") from exc

    if not report_path.is_file():
        detail = (completed.stdout or "")[-1000:].strip()
        if completed.stderr:
            detail = ((detail + "\n" if detail else "") + completed.stderr[-1000:].strip()).strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"LaTeX 校验未生成报告{suffix}")

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 compile-report.json: {exc}") from exc
    if not isinstance(report, dict):
        raise RuntimeError("compile-report.json 格式不正确")
    return report, log_path


def _error_contexts(report: dict[str, Any], log_text: str) -> list[tuple[int | None, str]]:
    contexts: list[tuple[int | None, str]] = []
    raw_errors = report.get("errors", [])
    if isinstance(raw_errors, list):
        for error in raw_errors:
            if not isinstance(error, str) or not error.strip():
                continue
            match = _LINE_RE.search(error)
            contexts.append((int(match.group(1)) if match else None, error.strip()))

    lines = log_text.splitlines()
    for index, line in enumerate(lines):
        match = _ERROR_LINE_RE.match(line)
        if not match:
            continue
        line_number = None
        for lookahead in lines[index + 1 : min(len(lines), index + 12)]:
            number_match = re.search(r"(?:^|\s)l\.(\d+)", lookahead)
            if number_match:
                line_number = int(number_match.group(1))
                break
        message = match.group(1).strip()
        if all(existing_message != message for _number, existing_message in contexts):
            contexts.append((line_number, message))
    return contexts


def _context_snippet(tex_text: str, contexts: list[tuple[int | None, str]]) -> str:
    lines = tex_text.splitlines(keepends=True)
    if not lines:
        return ""
    numbered_lines = [(index, f"{index + 1}: {line}") for index, line in enumerate(lines)]

    selected: set[int] = set()
    reported: list[str] = []
    for line_number, message in contexts:
        if message not in reported:
            reported.append(message)
        if line_number is None:
            continue
        center = max(0, min(line_number - 1, len(lines) - 1))
        half = _MAX_CONTEXT_LINES // 2
        for index in range(max(0, center - half), min(len(lines), center + half + 1)):
            selected.add(index)

    if not selected:
        selected.update(range(min(len(lines), _MAX_CONTEXT_LINES)))

    ordered = sorted(selected)
    chunks: list[str] = []
    previous: int | None = None
    for index in ordered:
        if previous is not None and index != previous + 1:
            chunks.append("...")
        chunks.append(numbered_lines[index][1])
        previous = index

    if len(ordered) > _MAX_CONTEXT_LINES:
        chunks = chunks[:_MAX_CONTEXT_LINES]
    if reported:
        chunks.insert(0, "LaTeX errors:\n" + "\n".join(f"- {message}" for message in reported))
    return "\n".join(chunks)


def _round_reason(report: dict[str, Any], exc: Exception | None = None) -> str:
    if exc is not None:
        return str(exc)
    errors = report.get("errors", [])
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    if report.get("timed_out"):
        return "LaTeX 编译超时"
    return "LaTeX 编译未通过"


def compile_with_repair(
    tex: Path,
    work_dir: Path,
    client: LLMClient | None,
    *,
    rounds: int = 5,
    log: Callable[[str], None] | None = None,
    progress: Callable[[float], None] | None = None,
) -> CompileOutcome:
    tex = Path(tex)
    work_dir = Path(work_dir)
    warnings_out: list[str] = []

    def emit(message: str) -> None:
        warnings_out.append(message)
        if log is not None:
            log(message)

    def notify(fraction: float) -> None:
        if progress is not None:
            progress(max(0.0, min(1.0, fraction)))

    try:
        report, log_path = compile_tex(tex, work_dir)
    except Exception as exc:
        report = _failed_report(tex, work_dir / f"{tex.stem}.log", str(exc))
        log_path = work_dir / f"{tex.stem}.log"

    rounds_used = 0
    if report.get("passed") is True or client is None or rounds <= 0:
        return CompileOutcome(
            passed=bool(report.get("passed")),
            rounds_used=rounds_used,
            report=report,
            tex=tex,
            log_path=log_path,
            warnings=warnings_out,
        )

    total_rounds = max(1, int(rounds))
    for round_number in range(1, total_rounds + 1):
        rounds_used = round_number
        notify((round_number - 1) / total_rounds)
        try:
            tex_text = tex.read_text(encoding="utf-8")
        except OSError as exc:
            emit(f"第 {round_number} 轮修复失败: 无法读取 TeX 文件 ({exc})")
            notify(round_number / total_rounds)
            break

        log_text = ""
        if log_path.is_file():
            try:
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                emit(f"第 {round_number} 轮修复失败: 无法读取编译日志 ({exc})")

        contexts = _error_contexts(report, log_text)
        if not contexts:
            contexts.append((None, _round_reason(report)))
        snippet = _context_snippet(tex_text, contexts)
        prompt = (
            "你是 LaTeX 编译错误修复助手。根据带行号的 TeX 片段修复明显 OCR 或语法错误。"
            "不要重写无关内容，不要删除数学信息。只返回严格 JSON，格式为："
            '{"patches":[{"find":"...","replace":"..."}],"reason":"..."}'
        )
        try:
            response = client.chat_json(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": snippet},
                ],
                temperature=0.0,
            )
        except LLMError as exc:
            emit(f"第 {round_number} 轮修复失败: {exc}")
            notify(round_number / total_rounds)
            if round_number < total_rounds:
                continue
            break
        except Exception as exc:
            emit(f"第 {round_number} 轮修复失败: {exc}")
            notify(round_number / total_rounds)
            if round_number < total_rounds:
                continue
            break

        patches: list[Any]
        reason = ""
        if isinstance(response, dict):
            raw_patches = response.get("patches", [])
            patches = raw_patches if isinstance(raw_patches, list) else []
            reason_value = response.get("reason", "")
            reason = reason_value if isinstance(reason_value, str) else str(reason_value)
        else:
            patches = []

        applicable: list[tuple[str, str]] = []
        for patch in patches:
            if not isinstance(patch, dict):
                continue
            find = patch.get("find")
            replace = patch.get("replace")
            if not isinstance(find, str) or not isinstance(replace, str) or find == "":
                continue
            occurrences = tex_text.count(find)
            if occurrences != 1:
                emit(f"第 {round_number} 轮跳过补丁: find 不唯一或不存在")
                continue
            applicable.append((find, replace))

        if not applicable:
            emit(f"第 {round_number} 轮没有可应用补丁: {reason or _round_reason(report)}")
            try:
                report, log_path = compile_tex(tex, work_dir)
            except Exception as exc:
                report = _failed_report(tex, log_path, str(exc))
            notify(round_number / total_rounds)
            if report.get("passed") is True:
                break
            if round_number == total_rounds:
                break
            continue

        backup = work_dir / f"backup-r{round_number}.tex"
        try:
            backup.write_text(tex_text, encoding="utf-8")
            current = tex_text
            for find, replace in applicable:
                if current.count(find) == 1:
                    current = current.replace(find, replace, 1)
            if current == tex_text:
                raise RuntimeError("补丁未改变 TeX 内容")
            tex.write_text(current, encoding="utf-8")
        except Exception as exc:
            emit(f"第 {round_number} 轮应用补丁失败: {exc}")
            try:
                report, log_path = compile_tex(tex, work_dir)
            except Exception as compile_exc:
                report = _failed_report(tex, log_path, str(compile_exc))
            notify(round_number / total_rounds)
            if round_number == total_rounds:
                break
            continue

        try:
            report, log_path = compile_tex(tex, work_dir)
        except Exception as exc:
            report = _failed_report(tex, log_path, str(exc))
            emit(f"第 {round_number} 轮重新编译失败: {exc}")

        notify(round_number / total_rounds)
        if report.get("passed") is True:
            break
        if round_number == total_rounds:
            emit(f"第 {round_number} 轮后仍未通过: {_round_reason(report)}")
        else:
            emit(f"第 {round_number} 轮修复后仍未通过: {_round_reason(report)}")

    return CompileOutcome(
        passed=bool(report.get("passed")),
        rounds_used=rounds_used,
        report=report,
        tex=tex,
        log_path=log_path,
        warnings=warnings_out,
    )

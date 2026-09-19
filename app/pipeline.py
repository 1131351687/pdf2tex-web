"""End-to-end PDF to Markdown/LaTeX orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.contracts import LLMSettings, STAGES, JobOptions, safe_stem

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "vendor" / "pdf2tex" / "scripts"

_MINERU_WRAPPER = """import os
import sys

sys.path.insert(0, sys.argv[1])
sys.argv = [sys.argv[0], *sys.argv[2:]]
import mineru_pipeline

base_url = os.environ.get("MINERU_BASE_URL")
if base_url:
    mineru_pipeline.BASE_URL = base_url
raise SystemExit(mineru_pipeline.main())
"""


class PipelineError(RuntimeError):
    """A user-facing pipeline failure."""


@dataclass
class PipelineResult:
    markdown: Path | None
    compat_markdown: Path | None
    tex: Path | None
    pdf: Path | None
    log_path: Path
    quality: dict[str, Any]
    warnings: list[str]
    needs_review: bool
    review_reasons: list[str]
    proofread: dict[str, Any] | None = None
    repair: dict[str, Any] | None = None


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str


CommandRunner = Callable[..., CompletedProcessLike]
StageTimes = dict[str, float]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _page_count(pdf: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(pdf)).pages)


def _run_command(
    command: list[str | Path],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    log_file: Path | None = None,
) -> CompletedProcessLike:
    printable = [str(item) for item in command]
    started = time.perf_counter()
    try:
        result = subprocess.run(
            printable,
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except OSError as exc:
        output = f"无法启动命令: {exc}"
        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(output + "\n", encoding="utf-8")
        raise PipelineError(f"无法执行转换步骤：{exc}") from exc

    output = result.stdout or ""
    elapsed = time.perf_counter() - started
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as stream:
            stream.write(f"+ {' '.join(printable)}\n")
            stream.write(output)
            stream.write(f"[exit={result.returncode}, seconds={elapsed:.3f}]\n")
    return result


def _default_proofread(
    text: str,
    client: Any,
    *,
    max_chars: int,
    max_chunks: int,
    log: Callable[[str], None] | None,
    progress: Callable[[float], None] | None,
) -> Any:
    from app.proofread import proofread_markdown

    return proofread_markdown(
        text,
        client,
        max_chars=max_chars,
        max_chunks=max_chunks,
        log=log,
        progress=progress,
    )


def _default_compile_with_repair(
    tex: Path,
    work_dir: Path,
    client: Any,
    *,
    rounds: int,
    log: Callable[[str], None] | None,
    progress: Callable[[float], None] | None,
) -> Any:
    from app.repair_loop import compile_with_repair

    return compile_with_repair(
        tex,
        work_dir,
        client,
        rounds=rounds,
        log=log,
        progress=progress,
    )


def _default_client(settings: LLMSettings | None) -> Any:
    if settings is None:
        return None
    from app.llm import build_client

    return build_client(settings)


def _is_cjk(language: str) -> bool:
    value = language.strip().lower()
    return value in {"ch", "zh", "zh-cn", "zh-tw", "ja", "jp", "ko"}


def _chunk_id(start: int, end: int) -> str:
    return f"pages-{start:04d}-{end:04d}"


def _split_range(start: int, end: int) -> list[tuple[int, int]]:
    middle = (start + end) // 2
    return [(start, middle), (middle + 1, end)]


def _plan_ranges(total_pages: int, chunk_size: int) -> list[tuple[int, int]]:
    return [
        (start, min(start + chunk_size - 1, total_pages))
        for start in range(1, total_pages + 1, chunk_size)
    ]


def _redact(value: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[已隐藏]")
    return value


class _Pipeline:
    # Progress is deliberately global and monotonic. The fraction supplied to the
    # callback therefore combines each stage's offset with its internal progress.
    _STAGE_BOUNDS: dict[str, tuple[float, float]] = {
        "prepare": (0.0, 0.06),
        "ocr": (0.06, 0.52),
        "merge": (0.52, 0.58),
        "repair": (0.58, 0.66),
        "proofread": (0.66, 0.72),
        "pandoc": (0.72, 0.80),
        "validate": (0.80, 0.88),
        "llm_repair": (0.88, 0.94),
        "finalize": (0.94, 1.0),
    }

    def __init__(
        self,
        *,
        job_dir: Path,
        pdf: Path,
        options: JobOptions,
        llm: LLMSettings | None,
        log: Callable[[str], None],
        progress: Callable[[str, str, float], None],
        command_runner: CommandRunner,
        page_count: Callable[[Path], int],
        proofread: Callable[..., Any] | None,
        compile_with_repair: Callable[..., Any] | None,
        client_factory: Callable[[LLMSettings | None], Any],
    ) -> None:
        self.job_dir = job_dir.resolve()
        self.pdf = pdf.resolve()
        self.options = options
        self.llm = llm
        self.log = log
        self.progress = progress
        self.run_command = command_runner
        self.page_count = page_count
        self.proofread_fn = proofread
        self.compile_with_repair_fn = compile_with_repair
        self.client_factory = client_factory
        self.book_dir = self.job_dir / "book"
        self.logs_dir = self.job_dir / "logs"
        self.chunks_dir = self.job_dir / "chunks"
        self.log_path = self.logs_dir / "job.log"
        self.source_hash = _sha256(self.pdf)
        self.pages = page_count(self.pdf)
        self.name = safe_stem(self.pdf.name, "book")
        self.chunks: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.stage_times: StageTimes = {}
        self.last_progress = 0.0
        self.secrets = [
            value.strip()
            for value in (
                os.environ.get("MINERU_API_TOKEN", ""),
                self.llm.api_key if self.llm else "",
            )
            if value.strip()
        ]

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.book_dir.mkdir(parents=True, exist_ok=True)
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        self._raw_log(f"任务目录: {self.job_dir}")
        self._raw_log(f"源文件: {self.pdf.name}")
        self._raw_log(f"页数: {self.pages}")

    def _raw_log(self, line: str) -> None:
        clean = _redact(str(line), self.secrets).replace("\r", "")
        if "\n" in clean:
            for part in clean.splitlines():
                self.log(part)
                with self.log_path.open("a", encoding="utf-8") as stream:
                    stream.write(part + "\n")
        else:
            self.log(clean)
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(clean + "\n")

    def _report(self, stage_key: str, fraction: float) -> None:
        start, end = self._STAGE_BOUNDS[stage_key]
        bounded = max(0.0, min(1.0, fraction))
        value = start + (end - start) * bounded
        value = max(self.last_progress, value)
        self.last_progress = value
        self.progress(stage_key, STAGES[stage_key], value)

    def _begin_stage(self, stage_key: str) -> None:
        self.stage_times[stage_key] = time.perf_counter()
        self._report(stage_key, 0.0)

    def _end_stage(self, stage_key: str) -> None:
        elapsed = time.perf_counter() - self.stage_times.get(stage_key, time.perf_counter())
        self.stage_times[stage_key] = elapsed
        self._report(stage_key, 1.0)

    def _execute(
        self,
        command: list[str | Path],
        log_name: str,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        required: bool = True,
    ) -> CompletedProcessLike:
        self._raw_log(f"执行: {Path(str(command[0])).name} {Path(str(command[-1])).name}")
        result = self.run_command(
            command,
            cwd=cwd,
            env=env,
            log_file=self.logs_dir / log_name,
        )
        if required and result.returncode != 0:
            tail = "\n".join((result.stdout or "").splitlines()[-20:])
            if tail:
                self._raw_log(tail)
            raise PipelineError(
                f"转换步骤执行失败（返回码 {result.returncode}）：{log_name}"
            )
        return result

    def _new_record(self, start: int, end: int, depth: int = 0) -> dict[str, Any]:
        label = _chunk_id(start, end)
        return {
            "id": label,
            "start": start,
            "end": end,
            "page_count": end - start + 1,
            "depth": depth,
            "subset_pdf": str(self.chunks_dir / f"{label}.pdf"),
            "manifest": str(self.chunks_dir / f"{label}.manifest.json"),
            "ocr_dir": str(self.chunks_dir / label / "attempt-01"),
            "status": "pending",
        }

    def _extract_chunk(
        self, record: dict[str, Any], subset_pdf: Path, manifest: Path
    ) -> None:
        self._execute(
            [
                sys.executable,
                SCRIPT_DIR / "extract_pdf_pages.py",
                self.pdf,
                subset_pdf,
                "--pages",
                f"{record['start']}-{record['end']}",
                "--manifest",
                manifest,
                "--overwrite",
            ],
            f"{record['id']}-extract.log",
        )

    def _reuse_chunk(self, record: dict[str, Any]) -> dict[str, Any] | None:
        chunk_dir = self.chunks_dir / record["id"]
        for candidate in sorted(chunk_dir.glob("attempt-*"), reverse=True):
            candidate_md = candidate / "result" / "full.md"
            state = _read_json(candidate / "state.json")
            if (
                candidate_md.is_file()
                and candidate_md.stat().st_size > 0
                and state.get("status") == "done"
                and state.get("source_sha256") == self.source_hash
                and state.get("language") == self.options.language
            ):
                record["ocr_dir"] = str(candidate)
                record["status"] = "reused"
                self._raw_log(f"chunk {record['id']}: 复用已有 OCR 结果")
                return record
        return None

    def _update_chunk_state(
        self, state_path: Path, record: dict[str, Any]
    ) -> None:
        state = _read_json(state_path)
        state.update(
            {
                "source_pdf": str(self.pdf),
                "source_sha256": self.source_hash,
                "source_page_count": self.pages,
                "source_pages": [record["start"], record["end"]],
                "language": self.options.language,
                "engine": "remote-mineru-v4",
            }
        )
        _write_json(state_path, state)

    def _run_mineru(self, subset_pdf: Path, ocr_dir: Path, attempt: int, label: str) -> None:
        ocr_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["MINERU_API_TOKEN"] = os.environ.get("MINERU_API_TOKEN", "")
        env["PYTHONPATH"] = os.pathsep.join(
            [str(SCRIPT_DIR), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        command = [
            sys.executable,
            "-c",
            _MINERU_WRAPPER,
            str(SCRIPT_DIR),
            "v4",
            str(subset_pdf),
            str(ocr_dir),
            "--language",
            self.options.language,
        ]
        self._execute(
            command,
            f"{label}-attempt-{attempt:02d}.log",
            cwd=SCRIPT_DIR,
            env=env,
        )

    def _process_chunk(
        self, start: int, end: int, depth: int = 0
    ) -> list[dict[str, Any]]:
        record = self._new_record(start, end, depth)
        reused = self._reuse_chunk(record)
        if reused is not None:
            return [reused]

        label = record["id"]
        subset_pdf = Path(record["subset_pdf"])
        manifest = Path(record["manifest"])
        self._extract_chunk(record, subset_pdf, manifest)

        attempts = 0
        last_error = ""
        max_attempts = max(1, self.options.retries + 1)
        while attempts < max_attempts:
            attempts += 1
            ocr_dir = self.chunks_dir / label / f"attempt-{attempts:02d}"
            state_path = ocr_dir / "state.json"
            result_md = ocr_dir / "result" / "full.md"
            try:
                self._run_mineru(subset_pdf, ocr_dir, attempts, label)
                if not result_md.is_file() or result_md.stat().st_size == 0:
                    raise RuntimeError("MinerU 完成但 result/full.md 缺失或为空")
                record.update(
                    status="done",
                    ocr_dir=str(ocr_dir),
                    attempts=attempts,
                )
                self._update_chunk_state(state_path, record)
                self._raw_log(f"chunk {label}: done")
                return [record]
            except Exception as exc:
                last_error = str(exc)
                self._raw_log(f"chunk {label}: 第 {attempts} 次尝试失败：{exc}")

        page_count = end - start + 1
        if page_count > self.options.min_chunk_pages and depth < self.options.max_depth:
            self._raw_log(f"chunk {label}: 失败后拆分重试")
            children: list[dict[str, Any]] = []
            for child_start, child_end in _split_range(start, end):
                children.extend(
                    self._process_chunk(child_start, child_end, depth + 1)
                )
            if all(item["status"] in {"done", "reused"} for item in children):
                return children

        record.update(status="failed", attempts=attempts, error=last_error)
        return [record]

    def _ocr(self) -> None:
        token = os.environ.get("MINERU_API_TOKEN", "").strip()
        if not token:
            raise PipelineError(
                "缺少 MINERU_API_TOKEN，请先在设置中填写 MinerU API Token。"
            )
        if self.options.chunk_size <= 0 or self.options.chunk_size > 200:
            raise PipelineError("分块页数必须在 1 到 200 页之间。")

        planned = _plan_ranges(self.pages, self.options.chunk_size)
        completed: list[dict[str, Any]] = []
        for index, (start, end) in enumerate(planned):
            records = self._process_chunk(start, end)
            completed.extend(records)
            within = (index + 1) / len(planned) if planned else 1.0
            self._report("ocr", within)
        failed = [record for record in completed if record.get("status") == "failed"]
        if failed:
            page_ranges = ", ".join(
                f"{record['start']}-{record['end']}" for record in failed
            )
            raise PipelineError(f"以下源页的 OCR 失败：{page_ranges}")
        self.chunks = completed

    def _merge(self) -> Path:
        blocks: list[dict[str, Any]] = []
        volumes: list[dict[str, Any]] = []
        for index, record in enumerate(
            sorted(self.chunks, key=lambda item: item["start"])
        ):
            ocr_dir = Path(record["ocr_dir"])
            blocks.append(
                {
                    "label": record["id"],
                    "md": str(ocr_dir / "result" / "full.md"),
                    "images": str(ocr_dir / "result" / "images"),
                }
            )
            volumes.append(
                {
                    "name": record["id"],
                    "file": "book.md",
                    "segments": [[record["id"], 1, None]],
                }
            )
        config = {
            "out": str(self.book_dir),
            "blocks": blocks,
            "volumes": volumes,
        }
        config_path = self.job_dir / "merge-volumes.json"
        _write_json(config_path, config)
        self._execute(
            [sys.executable, SCRIPT_DIR / "merge_volumes.py", config_path],
            "merge-volumes.log",
        )
        markdown = self.book_dir / "book.md"
        if not markdown.is_file():
            raise PipelineError("合并 Markdown 失败：未生成 book.md")
        return markdown

    def _repair_markdown(self, markdown: Path) -> tuple[Path, dict[str, Any]]:
        repaired = self.book_dir / "book.fixed.md"
        compat = self.book_dir / "book.compat.md"
        repair_report_path = self.book_dir / "repair-report.json"
        self._execute(
            [
                sys.executable,
                SCRIPT_DIR / "repair_latex.py",
                markdown,
                repaired,
                "--report",
                repair_report_path,
            ],
            "repair-latex.log",
        )
        self._execute(
            [
                sys.executable,
                SCRIPT_DIR / "fix_html_tables.py",
                repaired,
                "--patch-md",
                compat,
            ],
            "fix-html-tables.log",
        )
        report_path = self.book_dir / "validation" / "markdown-validation.json"
        result = self._execute(
            [
                sys.executable,
                SCRIPT_DIR / "validate_markdown.py",
                compat,
                "--report",
                report_path,
            ],
            "validate-markdown.log",
            required=False,
        )
        validation = _read_json(report_path)
        if not validation and result.returncode != 0:
            raise PipelineError("Markdown 校验失败，且未生成校验报告。")
        if validation.get("replacement_chars", 0):
            self.warnings.append("Markdown 中存在 Unicode 替换字符。")
        if validation.get("unbalanced_math_blocks", 0):
            self.warnings.append("Markdown 中存在不平衡的 $$ 数学块。")
        if validation.get("unbalanced_inline_math", 0):
            self.warnings.append("Markdown 中存在不平衡的行内数学定界符。")
        if validation.get("missing_image_count", 0):
            self.warnings.append("Markdown 中存在缺失的图片引用。")
        if not compat.is_file():
            raise PipelineError("Markdown 修复失败：未生成 book.compat.md")
        return compat, {"repair": _read_json(repair_report_path), "validation": validation}

    def _proofread(self, compat: Path, client: Any) -> dict[str, Any] | None:
        if not self.options.proofread or self.llm is None:
            return None
        proofread_fn = self.proofread_fn or _default_proofread
        try:
            outcome = proofread_fn(
                compat.read_text(encoding="utf-8"),
                client,
                max_chars=self.options.proofread_max_chars,
                max_chunks=self.options.proofread_max_chunks,
                log=self._raw_log,
                progress=lambda value: self._report("proofread", value),
            )
            markdown = getattr(outcome, "markdown", None)
            if isinstance(markdown, str):
                compat.write_text(markdown, encoding="utf-8", newline="\n")
            return {
                "total_chunks": getattr(outcome, "total_chunks", 0),
                "applied": getattr(outcome, "applied", 0),
                "skipped": getattr(outcome, "skipped", 0),
                "warnings": list(getattr(outcome, "warnings", [])),
            }
        except ValueError as exc:
            warning = f"LLM 校对已跳过：{exc}"
            self.warnings.append(warning)
            self._raw_log(warning)
            return None

    def _build_header(self) -> Path:
        path = self.book_dir / "pandoc-header.tex"
        path.write_text(
            "\n".join(
                [
                    "% Generated by pdf2tex-web",
                    "\\usepackage{fontspec}",
                    "\\usepackage{unicode-math}",
                    "\\usepackage{xeCJK}",
                    f"\\setmainfont{{{self.options.main_font}}}",
                    f"\\setsansfont{{{self.options.main_font}}}",
                    f"\\setmonofont{{{self.options.mono_font}}}",
                    f"\\setmathfont{{{self.options.math_font}}}",
                    f"\\setCJKmainfont{{{self.options.cjk_font}}}",
                    f"\\setCJKsansfont{{{self.options.cjk_font}}}",
                    f"\\setCJKmonofont{{{self.options.cjk_font}}}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def _pandoc(self, compat: Path) -> Path:
        if shutil.which("pandoc") is None:
            raise PipelineError("未找到 pandoc，请安装 Pandoc 后重试。")
        if shutil.which("xelatex") is None:
            raise PipelineError("未找到 xelatex，请安装 XeLaTeX 后重试。")
        header = self._build_header()
        tex = self.book_dir / f"{self.name}.tex"
        self._execute(
            [
                "pandoc",
                compat,
                "--standalone",
                "--pdf-engine=xelatex",
                "--resource-path",
                self.book_dir,
                "-H",
                header,
                "-o",
                tex,
            ],
            "pandoc-tex.log",
        )
        if not tex.is_file():
            raise PipelineError("Pandoc 未生成 TeX 文件。")
        return tex

    def _validate(
        self, tex: Path, client: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        validation_dir = self.book_dir / "validation"
        validation_dir.mkdir(parents=True, exist_ok=True)
        compile_fn = self.compile_with_repair_fn or _default_compile_with_repair
        outcome = compile_fn(
            tex,
            self.book_dir,
            client,
            rounds=self.options.repair_rounds,
            log=self._raw_log,
            progress=lambda value: self._report("validate", value),
        )
        report = dict(getattr(outcome, "report", {}))
        repair_summary = {
            "passed": bool(getattr(outcome, "passed", False)),
            "rounds_used": int(getattr(outcome, "rounds_used", 0)),
            "warnings": list(getattr(outcome, "warnings", [])),
            "report": report,
        }
        log_path = getattr(outcome, "log_path", None)
        if log_path is not None:
            repair_summary["log"] = str(log_path)
        _write_json(validation_dir / "compile-report.json", report)
        return report, repair_summary

    def _quality(
        self, compile_report: dict[str, Any], markdown_report: dict[str, Any]
    ) -> tuple[dict[str, Any], bool, list[str]]:
        if not compile_report:
            passed = not bool(
                markdown_report.get("replacement_chars", 0)
                or markdown_report.get("unbalanced_math_blocks", 0)
                or markdown_report.get("unbalanced_inline_math", 0)
            )
            quality = {
                "passed": passed,
                "markdown_validation": markdown_report,
            }
            reasons: list[str] = []
            if markdown_report.get("replacement_chars", 0):
                reasons.append("Markdown 中存在 Unicode 替换字符。")
            if markdown_report.get("unbalanced_math_blocks", 0):
                reasons.append("Markdown 中存在不平衡的 $$ 数学块。")
            if markdown_report.get("unbalanced_inline_math", 0):
                reasons.append("Markdown 中存在不平衡的行内数学定界符。")
            return quality, passed, reasons

        returncode = int(compile_report.get("returncode", -1))
        error_count = int(compile_report.get("error_count", 1))
        timed_out = bool(compile_report.get("timed_out", False))
        missing_glyph_count = int(compile_report.get("missing_glyph_count", 0))
        cjk = _is_cjk(self.options.language)
        passed = (
            compile_report.get("passed") is True
            and not timed_out
            and returncode == 0
            and error_count == 0
            and (not cjk or missing_glyph_count == 0)
        )
        reasons: list[str] = []
        if compile_report.get("passed") is not True:
            reasons.append("XeLaTeX 编译质量门未通过。")
        if timed_out:
            reasons.append("XeLaTeX 编译超时。")
        if returncode != 0:
            reasons.append("XeLaTeX 返回码非 0。")
        if error_count != 0:
            reasons.append("XeLaTeX 编译存在错误。")
        if cjk and missing_glyph_count != 0:
            reasons.append("CJK 文档存在缺失字形。")
        quality = {
            "passed": passed,
            "timed_out": timed_out,
            "returncode": returncode,
            "error_count": error_count,
            "missing_glyph_count": missing_glyph_count,
            "missing_glyph_count_required_zero": cjk,
            "markdown_validation": markdown_report,
        }
        return quality, passed, reasons

    def _summary(
        self,
        *,
        quality: dict[str, Any],
        needs_review: bool,
        review_reasons: list[str],
        markdown: Path | None,
        compat: Path | None,
        tex: Path | None,
        pdf: Path | None,
        proofread: dict[str, Any] | None,
        repair: dict[str, Any] | None,
    ) -> Path:
        outputs: dict[str, str] = {}
        for key, value in (
            ("markdown", markdown),
            ("compat_markdown", compat),
            ("tex", tex),
            ("pdf", pdf),
            ("log", self.log_path),
        ):
            if value is not None and value.exists():
                outputs[key] = str(value.relative_to(self.job_dir))
        data = {
            "source": {
                "filename": self.pdf.name,
                "sha256": self.source_hash,
                "page_count": self.pages,
            },
            "options": self.options.to_dict(),
            "stage_seconds": self.stage_times,
            "chunks": self.chunks,
            "warnings": self.warnings,
            "quality": quality,
            "needs_review": needs_review,
            "review_reasons": review_reasons,
            "proofread": proofread,
            "repair": repair,
            "outputs": outputs,
        }
        path = self.job_dir / "run-summary.json"
        _write_json(path, data)
        return path

    def run(self) -> PipelineResult:
        self._begin_stage("prepare")
        if self.options.chunk_size <= 0 or self.options.chunk_size > 200:
            raise PipelineError("分块页数必须在 1 到 200 页之间。")
        if self.pages <= 0:
            raise PipelineError("无法读取 PDF 页数，源文件可能无效。")
        self._end_stage("prepare")

        self._begin_stage("ocr")
        token = os.environ.get("MINERU_API_TOKEN", "").strip()
        if not token:
            raise PipelineError(
                "缺少 MINERU_API_TOKEN，请先在设置中填写 MinerU API Token。"
            )
        self._ocr()
        self._end_stage("ocr")

        self._begin_stage("merge")
        markdown = self._merge()
        self._end_stage("merge")

        self._begin_stage("repair")
        compat, markdown_reports = self._repair_markdown(markdown)
        self._end_stage("repair")

        client = self.client_factory(self.llm) if self.llm is not None else None
        proofread_summary: dict[str, Any] | None = None
        if self.options.proofread and self.llm is not None:
            self._begin_stage("proofread")
            proofread_summary = self._proofread(compat, client)
            self._end_stage("proofread")

        tex: Path | None = None
        pdf_path: Path | None = None
        compile_report: dict[str, Any] = {}
        repair_summary: dict[str, Any] | None = None
        if not self.options.no_pdf:
            self._begin_stage("pandoc")
            tex = self._pandoc(compat)
            self._end_stage("pandoc")

            self._begin_stage("validate")
            compile_report, repair_summary = self._validate(tex, client)
            self._end_stage("validate")

            if client is not None and self.options.llm_repair:
                self._begin_stage("llm_repair")
                self._raw_log(f"LLM 编译修复已使用轮次：{repair_summary['rounds_used']}")
                self._end_stage("llm_repair")
            pdf_path = self.book_dir / f"{self.name}.pdf"
            if not pdf_path.exists():
                pdf_path = None

        self._begin_stage("finalize")
        quality, passed, reasons = self._quality(compile_report, markdown_reports["validation"])
        needs_review = not passed
        final_markdown = self.book_dir / "book.md"
        summary_path = self._summary(
            quality=quality,
            needs_review=needs_review,
            review_reasons=reasons,
            markdown=final_markdown,
            compat=compat,
            tex=tex,
            pdf=pdf_path,
            proofread=proofread_summary,
            repair=repair_summary,
        )
        if needs_review:
            self._raw_log("质量门未通过，需要人工复核。")
        else:
            self._raw_log("转换完成。")
        self._report("finalize", 0.5)
        self._end_stage("finalize")
        _ = summary_path
        return PipelineResult(
            markdown=final_markdown,
            compat_markdown=compat,
            tex=tex,
            pdf=pdf_path,
            log_path=self.log_path,
            quality=quality,
            warnings=self.warnings,
            needs_review=needs_review,
            review_reasons=reasons,
            proofread=proofread_summary,
            repair=repair_summary,
        )


def run_pipeline(
    *,
    job_dir: Path,
    pdf: Path,
    options: JobOptions,
    llm: LLMSettings | None,
    log: Callable[[str], None],
    progress: Callable[[str, str, float], None],
    command_runner: CommandRunner | None = None,
    page_count: Callable[[Path], int] | None = None,
    proofread: Callable[..., Any] | None = None,
    compile_with_repair: Callable[..., Any] | None = None,
    client_factory: Callable[[LLMSettings | None], Any] | None = None,
) -> PipelineResult:
    """Run the frozen pipeline and return all generated conversion artifacts."""
    return _Pipeline(
        job_dir=job_dir,
        pdf=pdf,
        options=options,
        llm=llm,
        log=log,
        progress=progress,
        command_runner=command_runner or _run_command,
        page_count=page_count or _page_count,
        proofread=proofread,
        compile_with_repair=compile_with_repair,
        client_factory=client_factory or _default_client,
    ).run()

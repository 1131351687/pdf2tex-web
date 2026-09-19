"""Pipeline orchestration tests without network, MinerU, LLM, Pandoc, or XeLaTeX."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import pipeline as pipeline_module
from app.contracts import LLMSettings, JobOptions
from app.pipeline import PipelineError, run_pipeline


@pytest.fixture(autouse=True)
def _isolated_mineru_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _find_after(args: list[str], marker: str) -> str:
    return args[args.index(marker) + 1]


def _write_ocr_success(
    output: Path, source_hash: str, language: str = "ch"
) -> None:
    result = output / "result"
    result.mkdir(parents=True, exist_ok=True)
    (result / "full.md").write_text(
        f"# {output.name}\n\n$$x = 1$$\n", encoding="utf-8"
    )
    (output / "state.json").write_text(
        json.dumps(
            {
                "status": "done",
                "sha256": source_hash,
                "source_sha256": source_hash,
                "language": language,
            }
        ),
        encoding="utf-8",
    )


def _markdown_runner(source: Path, source_hash: str, fail_pages: set[str] | None = None):
    failures = fail_pages or set()
    calls: list[list[str]] = []

    def runner(command, *, cwd=None, env=None, log_file=None):
        args = [str(item) for item in command]
        calls.append(args)
        script = Path(args[1]).name if len(args) > 1 else ""

        if script == "extract_pdf_pages.py":
            output = Path(args[3])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"%PDF-1.4 fake subset\n")
            return SimpleNamespace(returncode=0, stdout="")

        if len(args) > 2 and args[1] == "-c":
            marker = args.index("v4")
            subset = Path(args[marker + 1])
            output = Path(args[marker + 2])
            if subset.stem in failures:
                return SimpleNamespace(returncode=1, stdout="fake MinerU failure")
            _write_ocr_success(output, source_hash)
            return SimpleNamespace(returncode=0, stdout="")

        if script == "merge_volumes.py":
            config = json.loads(Path(args[-1]).read_text(encoding="utf-8"))
            lines: list[str] = []
            for block in config["blocks"]:
                lines.extend(
                    Path(block["md"]).read_text(encoding="utf-8").splitlines()
                )
                lines.append("")
            Path(config["out"]).mkdir(parents=True, exist_ok=True)
            (Path(config["out"]) / "book.md").write_text(
                "\n".join(lines).strip() + "\n", encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout="")

        if script == "repair_latex.py":
            input_md = Path(args[2])
            output_md = Path(args[3])
            output_md.write_text(
                input_md.read_text(encoding="utf-8"), encoding="utf-8"
            )
            Path(args[5]).write_text(
                json.dumps({"tag_fixes": [], "array_fixes": []}), encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout="")

        if script == "fix_html_tables.py":
            input_md = Path(args[2])
            output_md = Path(_find_after(args, "--patch-md"))
            output_md.write_text(
                input_md.read_text(encoding="utf-8"), encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout="")

        if script == "validate_markdown.py":
            report_path = Path(_find_after(args, "--report"))
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "replacement_chars": 0,
                        "unbalanced_math_blocks": 0,
                        "unbalanced_inline_math": 0,
                        "missing_image_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="")

        if script == "pandoc" or args[0] == "pandoc":
            output = Path(_find_after(args, "-o"))
            output.write_text(
                "\\documentclass{article}\n\\begin{document}x\\end{document}\n",
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="")

        raise AssertionError(f"unexpected command: {args}")

    runner.calls = calls
    return runner


def _fake_compile(passed: bool = True):
    def compile_tex(tex: Path, work_dir: Path, client, *, rounds, log, progress):
        progress(0.5)
        report = {
            "passed": passed,
            "returncode": 0 if passed else 1,
            "timed_out": False,
            "error_count": 0 if passed else 2,
            "errors": [] if passed else ["Fake error"],
            "missing_glyph_count": 0,
            "preview_pdf_pages": 1,
        }
        (work_dir / f"{tex.stem}.pdf").write_bytes(b"%PDF-1.4 fake\n")
        log_path = work_dir / "validation" / "compile.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fake compile log\n", encoding="utf-8")
        return SimpleNamespace(
            passed=passed,
            rounds_used=0,
            report=report,
            log_path=log_path,
            warnings=[],
        )

    return compile_tex


def _progress_recorder():
    stages: list[str] = []
    fractions: list[float] = []

    def progress(stage_key: str, stage_label: str, fraction: float) -> None:
        stages.append(stage_key)
        fractions.append(fraction)

    return stages, fractions, progress


def _run(tmp_path: Path, *, options: JobOptions, llm=None, **kwargs):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake source\n")
    stages, fractions, progress = _progress_recorder()
    logs: list[str] = []
    result = run_pipeline(
        job_dir=tmp_path / "job",
        pdf=pdf,
        options=options,
        llm=llm,
        log=logs.append,
        progress=progress,
        page_count=lambda _pdf: 50,
        client_factory=lambda _settings: None,
        **kwargs,
    )
    return result, stages, fractions, logs


def test_pipeline_normal_completion_and_progress_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINERU_API_TOKEN", "mineru-secret-token")
    monkeypatch.setattr(pipeline_module.shutil, "which", lambda _name: "fake-tool")
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake source\n")
    runner = _markdown_runner(pdf, _sha(pdf))
    stages, fractions, progress = _progress_recorder()
    logs: list[str] = []

    result = run_pipeline(
        job_dir=tmp_path / "job",
        pdf=pdf,
        options=JobOptions(chunk_size=50, llm_repair=False),
        llm=None,
        log=logs.append,
        progress=progress,
        command_runner=runner,
        page_count=lambda _pdf: 50,
        compile_with_repair=_fake_compile(True),
        client_factory=lambda _settings: None,
    )

    assert result.needs_review is False
    assert result.quality["passed"] is True
    assert result.markdown is not None and result.markdown.is_file()
    assert result.compat_markdown is not None and result.compat_markdown.is_file()
    assert result.tex is not None and result.tex.is_file()
    assert result.pdf is not None and result.pdf.is_file()
    assert result.repair is not None and result.repair["passed"] is True
    assert (tmp_path / "job" / "run-summary.json").is_file()
    assert (tmp_path / "job" / "book" / "validation" / "compile-report.json").is_file()
    stage_order = list(dict.fromkeys(stages))
    assert stage_order == [
        "prepare",
        "ocr",
        "merge",
        "repair",
        "pandoc",
        "validate",
        "finalize",
    ]
    assert fractions[0] == 0.0
    assert fractions[-1] == 1.0
    assert all(0.0 <= value <= 1.0 for value in fractions)
    assert all(left <= right for left, right in zip(fractions, fractions[1:]))
    assert any("chunk pages-0001-0050: done" in line for line in logs)


def test_first_chunk_failure_is_split_and_children_retry_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINERU_API_TOKEN", "mineru-secret-token")
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake source\n")
    runner = _markdown_runner(pdf, _sha(pdf), fail_pages={"pages-0001-0050"})
    stages, fractions, progress = _progress_recorder()

    result, stages, fractions, logs = _run(
        tmp_path,
        options=JobOptions(
            chunk_size=50,
            min_chunk_pages=10,
            max_depth=1,
            retries=0,
            no_pdf=True,
        ),
        command_runner=runner,
    )

    assert result.needs_review is False
    assert result.tex is None and result.pdf is None
    mineru_calls = [call for call in runner.calls if len(call) > 2 and call[1] == "-c"]
    assert len(mineru_calls) == 3
    assert any("pages-0001-0050" in " ".join(call) for call in mineru_calls[:1])
    assert any("pages-0001-0025" in " ".join(call) for call in mineru_calls[1:])
    assert any("pages-0026-0050" in " ".join(call) for call in mineru_calls[1:])
    summary = json.loads(
        (tmp_path / "job" / "run-summary.json").read_text(encoding="utf-8")
    )
    assert [chunk["id"] for chunk in summary["chunks"]] == [
        "pages-0001-0025",
        "pages-0026-0050",
    ]
    assert all(chunk["status"] == "done" for chunk in summary["chunks"])
    assert list(dict.fromkeys(stages)) == [
        "prepare",
        "ocr",
        "merge",
        "repair",
        "finalize",
    ]
    assert all(0.0 <= value <= 1.0 for value in fractions)
    assert all(left <= right for left, right in zip(fractions, fractions[1:]))


def test_failed_quality_gate_returns_needs_review_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINERU_API_TOKEN", "mineru-secret-token")
    monkeypatch.setattr(pipeline_module.shutil, "which", lambda _name: "fake-tool")
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake source\n")
    runner = _markdown_runner(pdf, _sha(pdf))

    result, stages, fractions, _logs = _run(
        tmp_path,
        options=JobOptions(chunk_size=50, llm_repair=False),
        command_runner=runner,
        compile_with_repair=_fake_compile(False),
    )

    assert result.needs_review is True
    assert result.quality["passed"] is False
    assert result.quality["returncode"] == 1
    assert result.quality["error_count"] == 2
    assert "XeLaTeX 编译质量门未通过。" in result.review_reasons
    assert "XeLaTeX 返回码非 0。" in result.review_reasons
    assert "XeLaTeX 编译存在错误。" in result.review_reasons
    assert "finalize" in stages


def test_missing_mineru_token_raises_user_facing_error(tmp_path: Path) -> None:
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake source\n")

    with pytest.raises(PipelineError, match="MINERU_API_TOKEN"):
        run_pipeline(
            job_dir=tmp_path / "job",
            pdf=pdf,
            options=JobOptions(),
            llm=None,
            log=lambda _line: None,
            progress=lambda _stage, _label, _fraction: None,
            page_count=lambda _pdf: 50,
            client_factory=lambda _settings: None,
        )


def test_run_summary_and_logs_do_not_contain_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINERU_API_TOKEN", "super-secret-mineru-token")
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake source\n")
    runner = _markdown_runner(pdf, _sha(pdf))
    llm = LLMSettings(
        api_key="super-secret-llm-key",
        base_url="https://llm.example.test",
        model="fake-model",
    )
    stages, fractions, progress = _progress_recorder()
    logs: list[str] = []

    result = run_pipeline(
        job_dir=tmp_path / "job",
        pdf=pdf,
        options=JobOptions(chunk_size=50, no_pdf=True),
        llm=llm,
        log=logs.append,
        progress=progress,
        command_runner=runner,
        page_count=lambda _pdf: 50,
        client_factory=lambda _settings: None,
    )

    assert result.needs_review is False
    summary_path = tmp_path / "job" / "run-summary.json"
    summary_text = summary_path.read_text(encoding="utf-8")
    log_text = (tmp_path / "job" / "logs" / "job.log").read_text(encoding="utf-8")
    assert "super-secret-mineru-token" not in summary_text
    assert "super-secret-llm-key" not in summary_text
    assert "super-secret-mineru-token" not in log_text
    assert "super-secret-llm-key" not in log_text
    assert "MINERU_API_TOKEN" not in " ".join(
        args for call in runner.calls for args in call
    )


def test_ocr_parallel_chunks_preserve_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ocr_workers > 1 并发执行 MinerU 分块，且结果按页序合并。"""
    monkeypatch.setenv("MINERU_API_TOKEN", "mineru-secret-token")
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake source\n")

    barrier = threading.Barrier(2, timeout=10)
    lock = threading.Lock()
    concurrent_hits: list[str] = []

    def runner(command, *, cwd=None, env=None, log_file=None):
        args = [str(item) for item in command]
        script = Path(args[1]).name if len(args) > 1 else ""

        if script == "extract_pdf_pages.py":
            output = Path(args[3])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"%PDF-1.4 fake subset\n")
            return SimpleNamespace(returncode=0, stdout="")

        if len(args) > 2 and args[1] == "-c":
            marker = args.index("v4")
            subset = Path(args[marker + 1])
            output = Path(args[marker + 2])
            chunk_id = output.parent.name
            with lock:
                concurrent_hits.append(chunk_id)
            barrier.wait()
            result_dir = output / "result"
            result_dir.mkdir(parents=True, exist_ok=True)
            (result_dir / "full.md").write_text(
                f"# {chunk_id}\n\n$$x = 1$$\n", encoding="utf-8"
            )
            (output / "state.json").write_text(
                json.dumps(
                    {
                        "status": "done",
                        "source_sha256": _sha(pdf),
                        "language": "ch",
                    }
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="")

        if script == "merge_volumes.py":
            config = json.loads(Path(args[-1]).read_text(encoding="utf-8"))
            lines: list[str] = []
            for block in config["blocks"]:
                lines.extend(
                    Path(block["md"]).read_text(encoding="utf-8").splitlines()
                )
                lines.append("")
            Path(config["out"]).mkdir(parents=True, exist_ok=True)
            (Path(config["out"]) / "book.md").write_text(
                "\n".join(lines).strip() + "\n", encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout="")

        if script == "repair_latex.py":
            input_md = Path(args[2])
            output_md = Path(args[3])
            output_md.write_text(
                input_md.read_text(encoding="utf-8"), encoding="utf-8"
            )
            Path(args[5]).write_text(
                json.dumps({"tag_fixes": [], "array_fixes": []}), encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout="")

        if script == "fix_html_tables.py":
            input_md = Path(args[2])
            output_md = Path(_find_after(args, "--patch-md"))
            output_md.write_text(
                input_md.read_text(encoding="utf-8"), encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout="")

        if script == "validate_markdown.py":
            report_path = Path(_find_after(args, "--report"))
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "replacement_chars": 0,
                        "unbalanced_math_blocks": 0,
                        "unbalanced_inline_math": 0,
                        "missing_image_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="")

        raise AssertionError(f"unexpected command: {args}")

    result, _stages, _fractions, logs = _run(
        tmp_path,
        options=JobOptions(
            chunk_size=25,
            ocr_workers=2,
            no_pdf=True,
        ),
        command_runner=runner,
    )

    assert result.needs_review is False
    assert len(concurrent_hits) == 2
    summary = json.loads(
        (tmp_path / "job" / "run-summary.json").read_text(encoding="utf-8")
    )
    assert [chunk["id"] for chunk in summary["chunks"]] == [
        "pages-0001-0025",
        "pages-0026-0050",
    ]
    assert all(chunk["status"] == "done" for chunk in summary["chunks"])
    book_md = (tmp_path / "job" / "book" / "book.md").read_text(encoding="utf-8")
    assert book_md.index("pages-0001-0025") < book_md.index("pages-0026-0050")
    assert any("OCR 并发处理 2 个分块" in line for line in logs)

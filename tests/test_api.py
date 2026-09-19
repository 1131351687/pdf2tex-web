"""HTTP 层测试：设置、上传、任务状态、下载、SSE、重试。

用假的 pipeline runner 替换真实转换，测试不联网、不依赖 pandoc/xelatex。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.jobs import JobStore

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


@dataclass
class FakeResult:
    markdown: Path | None = None
    compat_markdown: Path | None = None
    tex: Path | None = None
    pdf: Path | None = None
    log_path: Path | None = None
    quality: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    proofread: dict | None = None
    repair: dict | None = None


def make_runner(*, needs_review: bool = False):
    def runner(*, job_dir: Path, pdf: Path, options, llm, log, progress) -> FakeResult:
        assert pdf.is_file(), "上传的 PDF 应该已经移动到任务目录"
        log("fake: 开始转换")
        progress("prepare", "准备", 0.0)
        progress("ocr", "MinerU OCR", 0.3)
        log("fake: OCR 完成")
        book = job_dir / "book"
        book.mkdir(parents=True, exist_ok=True)
        markdown = book / "book.md"
        markdown.write_text("# 标题\n\n$$x = 1$$\n", encoding="utf-8")
        compat = book / "book.compat.md"
        compat.write_text("# 标题\n\n$$x = 1$$\n", encoding="utf-8")
        tex = book / "book.tex"
        tex.write_text("\\documentclass{article}\\begin{document}x\\end{document}\n", encoding="utf-8")
        pdf_out = book / "book.pdf"
        pdf_out.write_bytes(PDF_BYTES)
        (job_dir / "run-summary.json").write_text(
            json.dumps({"quality": {"passed": not needs_review}}, ensure_ascii=False),
            encoding="utf-8",
        )
        progress("finalize", "整理产物", 1.0)
        return FakeResult(
            markdown=markdown,
            compat_markdown=compat,
            tex=tex,
            pdf=pdf_out,
            log_path=job_dir / "logs" / "job.log",
            quality={"passed": not needs_review, "error_count": 0},
            warnings=["示例警告"] if needs_review else [],
            needs_review=needs_review,
            review_reasons=["质量门未通过"] if needs_review else [],
            proofread={"total_chunks": 1, "applied": 1, "skipped": 0, "warnings": []},
            repair={"passed": not needs_review, "rounds_used": 0, "warnings": []},
        )

    return runner


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PDF2TEX_ENV_PATH", str(tmp_path / ".env"))
    # 宿主环境可能已经设置了 MinerU token，测试中必须隔离，否则行为不确定
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    store = JobStore(tmp_path / "jobs", runner=make_runner())
    monkeypatch.setattr(main_module, "store", store)
    with TestClient(main_module.app) as test_client:
        yield test_client, store


def _wait_terminal(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = client.get(f"/api/jobs/{job_id}").json()
        if detail["status"] in {"done", "needs_review", "failed"}:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内结束: {detail}")


def test_health_reports_tools(client) -> None:
    test_client, store = client
    body = test_client.get("/api/health").json()
    assert body["ok"] is True
    assert set(body["tools"]) == {"pandoc", "xelatex"}
    assert str(store.root) == body["data_dir"]


def test_settings_roundtrip_masks_secrets(client) -> None:
    test_client, _ = client
    response = test_client.put(
        "/api/settings",
        json={
            "env": {"MINERU_API_TOKEN": "mineru-token-1234", "LLM_API_KEY": "sk-abcdefgh"},
            "defaults": {"language": "en", "proofread": True},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mineru_token"]["set"] is True
    assert body["mineru_token"]["mask"].endswith("1234")
    assert "mineru-token-1234" not in json.dumps(body)
    assert body["defaults"]["language"] == "en"
    assert body["defaults"]["proofread"] is True

    assert test_client.put("/api/settings", json={"env": {"PATH": "x"}}).status_code == 400


def test_upload_rejects_non_pdf(client) -> None:
    test_client, _ = client
    response = test_client.post(
        "/api/jobs", files={"file": ("notes.txt", b"hello", "text/plain")}
    )
    assert response.status_code == 400
    response = test_client.post(
        "/api/jobs", files={"file": ("fake.pdf", b"not a pdf", "application/pdf")}
    )
    assert response.status_code == 400


def test_job_lifecycle_and_downloads(client) -> None:
    test_client, store = client
    test_client.put("/api/settings", json={"env": {"MINERU_API_TOKEN": "token-1234"}})

    response = test_client.post(
        "/api/jobs",
        files={"file": ("论文.pdf", PDF_BYTES, "application/pdf")},
        data={"options": json.dumps({"language": "ch", "proofread": True})},
    )
    assert response.status_code == 200
    record = response.json()
    job_id = record["id"]
    assert record["filename"] == "论文.pdf"

    detail = _wait_terminal(test_client, job_id)
    assert detail["status"] == "done"
    assert detail["progress"] == 1.0
    assert set(detail["outputs"]) >= {"markdown", "compat_markdown", "tex", "pdf", "log", "summary"}
    assert detail["proofread"]["applied"] == 1

    assert test_client.get("/api/jobs").json()["jobs"][0]["id"] == job_id

    for kind, content_type in (
        ("markdown", "text/markdown"),
        ("tex", "application/x-tex"),
        ("pdf", "application/pdf"),
        ("log", "text/plain"),
        ("summary", "application/json"),
    ):
        download = test_client.get(f"/api/jobs/{job_id}/download/{kind}")
        assert download.status_code == 200, kind
        assert content_type in download.headers["content-type"]

    assert test_client.get(f"/api/jobs/{job_id}/download/unknown").status_code == 400
    assert test_client.get("/api/jobs/nonexistent").status_code == 404

    retried = test_client.post(f"/api/jobs/{job_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "queued"
    assert _wait_terminal(test_client, job_id)["status"] == "done"

    # 任务目录里保存了原始 PDF 与汇总文件
    assert (store.job_dir(job_id) / "input.pdf").is_file()


def test_job_without_upload_artifact_kind(client) -> None:
    test_client, _ = client
    test_client.put("/api/settings", json={"env": {"MINERU_API_TOKEN": "token-1234"}})
    job_id = test_client.post(
        "/api/jobs", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")}
    ).json()["id"]
    _wait_terminal(test_client, job_id)

    # 缺省 runner 不会产出 compat_markdown 之外的额外键；这里验证未知 kind 的报错
    assert test_client.get(f"/api/jobs/{job_id}/download/other").status_code == 400


def test_missing_token_fails_job(client) -> None:
    test_client, _ = client
    job_id = test_client.post(
        "/api/jobs", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")}
    ).json()["id"]
    detail = _wait_terminal(test_client, job_id)
    assert detail["status"] == "failed"
    assert "MinerU" in detail["errors"][0]


def test_needs_review_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDF2TEX_ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    store = JobStore(tmp_path / "jobs", runner=make_runner(needs_review=True))
    monkeypatch.setattr(main_module, "store", store)
    with TestClient(main_module.app) as test_client:
        test_client.put("/api/settings", json={"env": {"MINERU_API_TOKEN": "token-1234"}})
        job_id = test_client.post(
            "/api/jobs", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")}
        ).json()["id"]
        detail = _wait_terminal(test_client, job_id)
        assert detail["status"] == "needs_review"
        assert "质量门未通过" in detail["message"]


def test_sse_emits_status(client) -> None:
    test_client, _ = client
    test_client.put("/api/settings", json={"env": {"MINERU_API_TOKEN": "token-1234"}})
    job_id = test_client.post(
        "/api/jobs", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")}
    ).json()["id"]
    _wait_terminal(test_client, job_id)

    with test_client.stream("GET", f"/api/jobs/{job_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        first_event = None
        for line in response.iter_lines():
            if line.startswith("data: "):
                first_event = json.loads(line[len("data: ") :])
                break
    assert first_event is not None
    assert first_event["type"] == "status"
    assert first_event["job"]["id"] == job_id


def test_sse_unknown_job_is_404(client) -> None:
    test_client, _ = client
    assert test_client.get("/api/jobs/missing/events").status_code == 404

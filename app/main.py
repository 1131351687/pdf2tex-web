"""FastAPI 应用：REST 接口、SSE 事件流、静态页面与产物下载。"""

from __future__ import annotations

import asyncio
import json
import platform
import queue
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .contracts import (
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    JobOptions,
    safe_stem,
)
from .jobs import JobStore, data_dir
from .settings import load_settings, save_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

DOWNLOAD_META: dict[str, tuple[str, str]] = {
    "markdown": ("text/markdown; charset=utf-8", ".md"),
    "compat_markdown": ("text/markdown; charset=utf-8", ".md"),
    "tex": ("application/x-tex; charset=utf-8", ".tex"),
    "pdf": ("application/pdf", ".pdf"),
    "log": ("text/plain; charset=utf-8", ".log"),
    "summary": ("application/json; charset=utf-8", ".json"),
}

store = JobStore(data_dir())


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    store.start()
    try:
        yield
    finally:
        store.stop()


app = FastAPI(title="pdf2tex-web", version=__version__, lifespan=lifespan)
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/", include_in_schema=False, response_model=None)
def index() -> Any:
    if not INDEX_FILE.is_file():
        return HTMLResponse(
            "<h1>pdf2tex-web</h1><p>前端页面缺失，请检查 app/static/index.html。</p>",
            status_code=503,
        )
    return FileResponse(INDEX_FILE, media_type="text/html; charset=utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "python": platform.python_version(),
        "tools": {
            "pandoc": bool(shutil.which("pandoc")),
            "xelatex": bool(shutil.which("xelatex")),
        },
        "data_dir": str(store.root),
    }


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return load_settings().public_dict()


@app.put("/api/settings")
async def put_settings(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")
    env_updates = body.get("env") or {}
    defaults = body.get("defaults") or {}
    if not isinstance(env_updates, dict) or not isinstance(defaults, dict):
        raise HTTPException(status_code=400, detail="env 与 defaults 必须是对象")
    try:
        saved = save_settings(env_updates=env_updates, defaults=defaults)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入 .env 失败: {exc}")
    return saved.public_dict()


@app.get("/api/jobs")
def list_jobs() -> dict[str, Any]:
    return {"jobs": [record.to_dict() for record in store.list_jobs()]}


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    options: str | None = Form(default=None),
) -> dict[str, Any]:
    filename = (file.filename or "").strip()
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="目前只支持 PDF 文件")

    parsed_options: dict[str, Any] = {}
    if options:
        try:
            parsed_options = json.loads(options)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="options 不是合法 JSON")
        if not isinstance(parsed_options, dict):
            raise HTTPException(status_code=400, detail="options 必须是对象")

    base = load_settings().defaults.to_dict()
    job_options = JobOptions.from_dict({**base, **parsed_options})

    incoming_dir = store.root / "_incoming"
    incoming_dir.mkdir(parents=True, exist_ok=True)
    temp_path = incoming_dir / f"{uuid.uuid4().hex}.pdf"
    total = 0
    header = b""
    try:
        with temp_path.open("wb") as stream:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="文件超过 500MB 上限")
                if len(header) < 5:
                    header += chunk[: 5 - len(header)]
                stream.write(chunk)
    except HTTPException:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    if total == 0:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="上传文件为空")
    if not header.startswith(b"%PDF-"):
        temp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="文件不是有效的 PDF")

    record = store.create_job(filename=filename, source=temp_path, options=job_options)
    return record.to_dict()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return record.to_dict()


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict[str, Any]:
    try:
        record = store.retry(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return record.to_dict()


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    subscription = store.subscribe(job_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    subscriber = subscription.subscriber

    async def event_stream() -> AsyncIterator[str]:
        try:
            yield _sse({"type": "status", "job": subscription.job.to_dict()})
            if subscription.terminal:
                return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.to_thread(subscriber.get, True, 5.0)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                yield _sse(event)
                if event.get("type") == "status":
                    status = (event.get("job") or {}).get("status")
                    if status in TERMINAL_STATUSES:
                        break
        finally:
            store.unsubscribe(job_id, subscriber)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str) -> FileResponse:
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if kind not in DOWNLOAD_META:
        raise HTTPException(status_code=400, detail=f"不支持的产物类型: {kind}")
    relative = record.outputs.get(kind)
    if not relative:
        raise HTTPException(status_code=404, detail="该产物尚未生成")

    job_dir = store.job_dir(job_id).resolve()
    target = (job_dir / relative).resolve()
    if job_dir != target and job_dir not in target.parents:
        raise HTTPException(status_code=400, detail="产物路径非法")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="产物文件不存在")

    media_type, suffix = DOWNLOAD_META[kind]
    return FileResponse(
        target, media_type=media_type, filename=f"{safe_stem(record.filename)}{suffix}"
    )


__all__ = ["app", "store", "STATUS_RUNNING"]

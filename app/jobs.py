"""任务队列、状态机与事件总线。

单进程、单工作线程：本地单用户场景下同时只跑一个转换任务，其余排队。
每次状态变化都会原子写入 `data/jobs/<id>/job.json`，重启后历史任务仍可查看。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .contracts import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STAGES,
    JobOptions,
    JobRecord,
    safe_stem,
)
from .settings import PROJECT_ROOT, load_settings

Runner = Callable[..., Any]

DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "jobs"
MAX_LOG_LINES = 5000


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def data_dir() -> Path:
    override = os.environ.get("PDF2TEX_DATA_DIR", "").strip()
    return Path(override) if override else DEFAULT_DATA_DIR


def _default_runner(**kwargs: Any) -> Any:
    """延迟导入，避免并行开发阶段模块缺失导致应用无法启动。"""
    from . import pipeline

    return pipeline.run_pipeline(**kwargs)


class EventBus:
    """每个任务一条广播通道，SSE 订阅者各自持有一个队列。"""

    def __init__(self) -> None:
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=4096)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                # 慢消费者丢掉最旧事件，保证后端不被阻塞
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass


@dataclass
class Subscription:
    job: JobRecord
    subscriber: queue.Queue[dict[str, Any]]
    terminal: bool


class JobStore:
    def __init__(self, root: Path | None = None, *, runner: Runner | None = None) -> None:
        self.root = Path(root or data_dir())
        self.root.mkdir(parents=True, exist_ok=True)
        self._runner: Runner = runner or _default_runner
        self._records: dict[str, JobRecord] = {}
        self._buses: dict[str, EventBus] = {}
        self._log_buffers: dict[str, list[str]] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self.load_all()

    # ------------------------------------------------------------------ 基础
    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def source_pdf(self, job_id: str, filename: str) -> Path:
        """任务目录里源 PDF 的落盘路径；保留原文件名，产物命名才能与上传文件一致。"""
        return self.job_dir(job_id) / f"{safe_stem(filename, 'input')}.pdf"

    def _bus(self, job_id: str) -> EventBus:
        with self._lock:
            bus = self._buses.get(job_id)
            if bus is None:
                bus = EventBus()
                self._buses[job_id] = bus
            return bus

    def load_all(self) -> None:
        for path in sorted(self.root.glob("*/job.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                record = JobRecord.from_dict(data)
            except (OSError, ValueError, TypeError):
                continue
            if not record.id:
                record.id = path.parent.name
            with self._lock:
                self._records[record.id] = record
            log_file = path.parent / "logs" / "job.log"
            if log_file.is_file():
                try:
                    tail = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    tail = []
                if tail:
                    with self._lock:
                        self._log_buffers[record.id] = tail[-MAX_LOG_LINES:]

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._worker = threading.Thread(
                target=self._run_worker, name="pdf2tex-worker", daemon=True
            )
            self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._queue.put(None)
        worker = self._worker
        if worker is not None:
            worker.join(timeout=timeout)
        self._worker = None

    def _run_worker(self) -> None:
        while not self._stop.is_set():
            job_id = self._queue.get()
            if job_id is None:
                break
            try:
                self._execute(job_id)
            except Exception as exc:  # 兜底，绝不让工作线程退出
                self._fail(job_id, f"内部错误: {exc}")
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ 查询
    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            record = self._records.get(job_id)
            return JobRecord.from_dict(record.to_dict()) if record else None

    def list_jobs(self) -> list[JobRecord]:
        with self._lock:
            records = [
                JobRecord.from_dict(record.to_dict()) for record in self._records.values()
            ]
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    def log_lines(self, job_id: str) -> list[str]:
        with self._lock:
            return list(self._log_buffers.get(job_id, []))

    def subscribe(self, job_id: str) -> Subscription | None:
        record = self.get(job_id)
        if record is None:
            return None
        return Subscription(
            job=record, subscriber=self._bus(job_id).subscribe(), terminal=record.terminal
        )

    def unsubscribe(self, job_id: str, subscriber: queue.Queue[dict[str, Any]]) -> None:
        self._bus(job_id).unsubscribe(subscriber)

    # ------------------------------------------------------------------ 写入
    def create_job(self, filename: str, source: Path, options: JobOptions) -> JobRecord:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        job_id = f"{stamp}-{uuid.uuid4().hex[:6]}"
        directory = self.job_dir(job_id)
        (directory / "logs").mkdir(parents=True, exist_ok=True)
        target = self.source_pdf(job_id, filename)
        source.replace(target)
        record = JobRecord(
            id=job_id,
            filename=filename,
            status=STATUS_QUEUED,
            stage="prepare",
            stage_label=STAGES["prepare"],
            progress=0.0,
            message="排队中",
            created_at=_now(),
            updated_at=_now(),
            options=options.to_dict(),
        )
        with self._lock:
            self._records[job_id] = record
            self._log_buffers[job_id] = []
        self._persist(record)
        self._queue.put(job_id)
        self.start()
        return record

    def retry(self, job_id: str) -> JobRecord:
        record = self.get(job_id)
        if record is None:
            raise KeyError(job_id)
        if record.terminal is False:
            return record
        pdf = self.source_pdf(job_id, record.filename)
        if not pdf.is_file():
            raise FileNotFoundError("原始 PDF 不存在，无法重试")
        record.status = STATUS_QUEUED
        record.stage = "prepare"
        record.stage_label = STAGES["prepare"]
        record.progress = 0.0
        record.message = "重新排队"
        record.errors = []
        record.warnings = []
        record.updated_at = _now()
        with self._lock:
            self._records[job_id] = record
            self._log_buffers[job_id] = []
        self._persist(record)
        self._publish_status(record)
        self._queue.put(job_id)
        self.start()
        return record

    def _persist(self, record: JobRecord) -> None:
        path = self.job_dir(record.id) / "job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        tmp.replace(path)

    def _publish_status(self, record: JobRecord) -> None:
        self._bus(record.id).publish({"type": "status", "job": record.to_dict()})

    def _set(self, record: JobRecord, **fields: Any) -> JobRecord:
        with self._lock:
            for key, value in fields.items():
                setattr(record, key, value)
            record.updated_at = _now()
            self._records[record.id] = record
        self._persist(record)
        self._publish_status(record)
        return record

    def _log(self, job_id: str, line: str, level: str = "info") -> None:
        text = line.rstrip("\n")
        if not text:
            return
        with self._lock:
            buffer = self._log_buffers.setdefault(job_id, [])
            buffer.append(text)
            if len(buffer) > MAX_LOG_LINES:
                del buffer[: len(buffer) - MAX_LOG_LINES]
        log_file = self.job_dir(job_id) / "logs" / "job.log"
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(f"[{_now()}] {text}\n")
        except OSError:
            pass
        self._bus(job_id).publish({"type": "log", "line": text, "level": level})

    # ------------------------------------------------------------------ 执行
    def _execute(self, job_id: str) -> None:
        record = self.get(job_id)
        if record is None:
            return
        options = JobOptions.from_dict(record.options)
        settings = load_settings()
        if not settings.mineru_token:
            self._fail(job_id, "缺少 MinerU API Token，请先在设置中填写并保存")
            return

        previous_token = os.environ.get("MINERU_API_TOKEN")
        os.environ["MINERU_API_TOKEN"] = settings.mineru_token
        llm = settings.llm if settings.llm.enabled else None

        def log(line: str) -> None:
            self._log(job_id, line)

        def progress(stage: str, label: str, fraction: float) -> None:
            current = self.get(job_id)
            if current is None:
                return
            value = max(0.0, min(1.0, float(fraction)))
            self._set(
                current,
                status=STATUS_RUNNING,
                stage=stage,
                stage_label=label or STAGES.get(stage, stage),
                progress=value,
            )

        self._set(
            record,
            status=STATUS_RUNNING,
            stage="prepare",
            stage_label=STAGES["prepare"],
            progress=0.0,
            message="开始转换",
        )
        self._log(job_id, f"任务开始：{record.filename}")
        if llm is None:
            self._log(job_id, "未配置 LLM API，跳过 LLM 校对与编译修复")
        try:
            result = self._runner(
                job_dir=self.job_dir(job_id),
                pdf=self.source_pdf(job_id, record.filename),
                options=options,
                llm=llm,
                log=log,
                progress=progress,
            )
            self._finalize(job_id, result)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self._fail(job_id, message)
        finally:
            if previous_token is None:
                os.environ.pop("MINERU_API_TOKEN", None)
            else:
                os.environ["MINERU_API_TOKEN"] = previous_token

    def _finalize(self, job_id: str, result: Any) -> None:
        record = self.get(job_id)
        if record is None:
            return
        job_dir = self.job_dir(job_id)
        outputs: dict[str, str] = {}
        mapping = {
            "markdown": getattr(result, "markdown", None),
            "compat_markdown": getattr(result, "compat_markdown", None),
            "tex": getattr(result, "tex", None),
            "pdf": getattr(result, "pdf", None),
            "log": job_dir / "logs" / "job.log",
            "summary": job_dir / "run-summary.json",
        }
        for key, value in mapping.items():
            if value is None:
                continue
            path = Path(value)
            if not path.is_file():
                continue
            try:
                outputs[key] = str(path.relative_to(job_dir)).replace("\\", "/")
            except ValueError:
                continue

        needs_review = bool(getattr(result, "needs_review", False))
        warnings = list(getattr(result, "warnings", []) or [])
        reasons = list(getattr(result, "review_reasons", []) or [])
        self._set(
            record,
            status=STATUS_NEEDS_REVIEW if needs_review else STATUS_DONE,
            stage="finalize",
            stage_label=STAGES["finalize"],
            progress=1.0,
            outputs=outputs,
            quality=dict(getattr(result, "quality", {}) or {}),
            warnings=warnings,
            errors=[],
            proofread=getattr(result, "proofread", None),
            llm_repair=getattr(result, "repair", None),
            message=(
                "转换完成，但有需要人工复核的项目：" + "；".join(reasons)
                if needs_review and reasons
                else ("转换完成，建议人工复核" if needs_review else "转换完成")
            ),
        )
        self._log(job_id, f"任务结束：{record.status}")

    def _fail(self, job_id: str, message: str) -> None:
        record = self.get(job_id)
        if record is None:
            return
        self._set(
            record,
            status=STATUS_FAILED,
            progress=1.0,
            errors=[message],
            message=message,
        )
        self._log(job_id, f"任务失败：{message}", level="error")

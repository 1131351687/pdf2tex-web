"""冻结的数据契约：流水线、任务层和 HTTP 层共用。

字段变更必须同步 docs/INTERFACES.md。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# 阶段键 -> 中文标签
STAGES: dict[str, str] = {
    "prepare": "准备",
    "ocr": "MinerU OCR",
    "merge": "合并 Markdown",
    "repair": "结构修复",
    "proofread": "LLM 校对",
    "pandoc": "生成 LaTeX",
    "validate": "XeLaTeX 编译",
    "llm_repair": "LLM 修复编译错误",
    "finalize": "整理产物",
}

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_NEEDS_REVIEW, STATUS_FAILED})

OUTPUT_KEYS = ("markdown", "compat_markdown", "tex", "pdf", "log", "summary")


@dataclass(frozen=True)
class LLMSettings:
    """OpenAI 兼容服务的连接参数。api_key 绝不写入日志或任务文件。"""

    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    timeout: float = 120.0
    max_retries: int = 2
    extra_body: dict[str, Any] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip()) and bool(self.base_url.strip())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JobOptions:
    """单次转换任务的参数。"""

    language: str = "ch"
    chunk_size: int = 50
    min_chunk_pages: int = 10
    max_depth: int = 4
    retries: int = 1
    ocr_workers: int = 1
    proofread: bool = False
    proofread_max_chars: int = 12000
    proofread_max_chunks: int = 800
    proofread_workers: int = 1
    llm_repair: bool = True
    repair_rounds: int = 5
    cjk_font: str = "SimSun"
    main_font: str = "DejaVu Sans"
    mono_font: str = "DejaVu Sans Mono"
    math_font: str = "Cambria Math"
    no_pdf: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> JobOptions:
        """只接受已知字段，忽略未知键，并按类型做保守转换。"""
        data = data or {}
        defaults = cls()
        values: dict[str, Any] = {}
        for name, default in defaults.to_dict().items():
            if name not in data:
                continue
            raw = data[name]
            if raw is None:
                continue
            if isinstance(default, bool):
                if isinstance(raw, str):
                    values[name] = raw.strip().lower() in {"1", "true", "yes", "on"}
                else:
                    values[name] = bool(raw)
            elif isinstance(default, int):
                try:
                    values[name] = int(raw)
                except (TypeError, ValueError):
                    continue
            elif isinstance(default, float):
                try:
                    values[name] = float(raw)
                except (TypeError, ValueError):
                    continue
            else:
                text = str(raw).strip()
                if text:
                    values[name] = text
        return cls(**values)


@dataclass
class JobRecord:
    """任务状态。序列化后写入 data/jobs/<id>/job.json。"""

    id: str
    filename: str
    status: str = STATUS_QUEUED
    stage: str = "prepare"
    stage_label: str = STAGES["prepare"]
    progress: float = 0.0
    message: str = ""
    created_at: str = ""
    updated_at: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    proofread: dict[str, Any] | None = None
    llm_repair: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobRecord:
        known = {
            "id",
            "filename",
            "status",
            "stage",
            "stage_label",
            "progress",
            "message",
            "created_at",
            "updated_at",
            "options",
            "outputs",
            "quality",
            "warnings",
            "errors",
            "proofread",
            "llm_repair",
        }
        return cls(**{key: value for key, value in data.items() if key in known})

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def mask_secret(value: str | None) -> str:
    """把密钥变成可展示的掩码：保留末 4 位，其余打星。"""
    text = (value or "").strip()
    if not text:
        return ""
    tail = text[-4:] if len(text) > 4 else text
    return "*" * max(6, len(text) - len(tail)) + tail


_SAFE_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_stem(filename: str, fallback: str = "document") -> str:
    """把上传文件名转换成安全的基名（不含扩展名）。"""
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    stem = _SAFE_NAME.sub("_", stem).strip(" ._")
    return stem or fallback

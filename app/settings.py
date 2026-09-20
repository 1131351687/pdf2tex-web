"""配置读写。

`.env` 是配置的权威来源：界面上保存的密钥与默认参数写回该文件。
进程环境变量只在 `.env` 缺少对应键时兜底（方便容器或 CI 注入默认值）。
密钥永远不会被写进日志、任务文件或 HTTP 响应（响应只给掩码）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .contracts import JobOptions, LLMSettings, mask_secret

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

# 界面可写的密钥类键
SECRET_KEYS = ("MINERU_API_TOKEN", "LLM_API_KEY")
CONFIG_KEYS = ("LLM_BASE_URL", "LLM_MODEL")
ALLOWED_ENV_KEYS = frozenset((*SECRET_KEYS, *CONFIG_KEYS))

# 默认参数键 -> JobOptions 字段
DEFAULT_KEY_MAP: dict[str, str] = {
    "PDF2TEX_LANGUAGE": "language",
    "PDF2TEX_CJK_FONT": "cjk_font",
    "PDF2TEX_MAIN_FONT": "main_font",
    "PDF2TEX_MONO_FONT": "mono_font",
    "PDF2TEX_MATH_FONT": "math_font",
    "PDF2TEX_PROOFREAD": "proofread",
    "PDF2TEX_LLM_REPAIR": "llm_repair",
    "PDF2TEX_REPAIR_ROUNDS": "repair_rounds",
    "PDF2TEX_CHUNK_SIZE": "chunk_size",
    "PDF2TEX_OCR_WORKERS": "ocr_workers",
    "PDF2TEX_PROOFREAD_WORKERS": "proofread_workers",
}
ALLOWED_DEFAULT_KEYS = frozenset(DEFAULT_KEY_MAP)

FILE_HEADER = "# pdf2tex-web 配置；包含密钥，请勿提交或分享\n"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}


def env_path() -> Path:
    """允许用 PDF2TEX_ENV_PATH 覆盖配置文件位置（测试用）。"""
    override = os.environ.get("PDF2TEX_ENV_PATH", "").strip()
    return Path(override) if override else DEFAULT_ENV_PATH


def _bool_value(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return default


def _int_value(raw: Any, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """读取 .env；文件不存在时返回空字典。"""
    target = path or env_path()
    if not target.is_file():
        return {}
    values = dotenv_values(target)
    return {key: (value or "") for key, value in values.items() if key}


def _effective(key: str, file_values: dict[str, str]) -> str:
    """文件值优先，缺失时回落到进程环境变量。"""
    if key in file_values and file_values[key].strip():
        return file_values[key].strip()
    return (os.environ.get(key) or "").strip()


@dataclass(frozen=True)
class AppSettings:
    mineru_token: str
    llm: LLMSettings
    defaults: JobOptions
    port: int = 7860

    def public_dict(self) -> dict[str, Any]:
        """返回给前端的安全视图（只含掩码）。"""
        defaults = self.defaults.to_dict()
        return {
            "mineru_token": {
                "set": bool(self.mineru_token),
                "mask": mask_secret(self.mineru_token),
            },
            "llm": {
                "api_key": {
                    "set": bool(self.llm.api_key),
                    "mask": mask_secret(self.llm.api_key),
                },
                "base_url": self.llm.base_url,
                "model": self.llm.model,
            },
            "defaults": {
                "language": defaults["language"],
                "proofread": defaults["proofread"],
                "llm_repair": defaults["llm_repair"],
                "repair_rounds": defaults["repair_rounds"],
                "chunk_size": defaults["chunk_size"],
                "ocr_workers": defaults["ocr_workers"],
                "proofread_workers": defaults["proofread_workers"],
                "cjk_font": defaults["cjk_font"],
                "main_font": defaults["main_font"],
                "mono_font": defaults["mono_font"],
                "math_font": defaults["math_font"],
            },
            "port": self.port,
        }


def load_settings(path: Path | None = None) -> AppSettings:
    file_values = read_env_file(path)
    extra_body: dict[str, Any] = {}
    extra_raw = _effective("PDF2TEX_LLM_EXTRA_BODY", file_values)
    if extra_raw:
        try:
            parsed = json.loads(extra_raw)
            if isinstance(parsed, dict):
                extra_body = parsed
        except json.JSONDecodeError:
            pass
    llm = LLMSettings(
        api_key=_effective("LLM_API_KEY", file_values),
        base_url=_effective("LLM_BASE_URL", file_values) or "https://api.deepseek.com",
        model=_effective("LLM_MODEL", file_values) or "deepseek-chat",
        extra_body=extra_body,
    )
    defaults = JobOptions()
    overrides: dict[str, Any] = {}
    for env_key, field_name in DEFAULT_KEY_MAP.items():
        raw = _effective(env_key, file_values)
        if not raw:
            continue
        current = getattr(defaults, field_name)
        if isinstance(current, bool):
            overrides[field_name] = _bool_value(raw, current)
        elif isinstance(current, int):
            overrides[field_name] = _int_value(raw, current)
        else:
            overrides[field_name] = raw
    port = _int_value(_effective("PDF2TEX_PORT", file_values) or "7860", 7860)
    return AppSettings(
        mineru_token=_effective("MINERU_API_TOKEN", file_values),
        llm=llm,
        defaults=defaults.__class__(**{**defaults.to_dict(), **overrides}),
        port=port,
    )


def _render_env(existing_lines: list[str], updates: dict[str, str]) -> str:
    """在保留注释与顺序的前提下更新键值；None 表示删除该键。"""
    pending = dict(updates)
    lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", stripped)
        if not match or match.group(1) not in pending:
            lines.append(line)
            continue
        key = match.group(1)
        value = pending.pop(key)
        if value is None:
            continue
        lines.append(f"{key}={value}")
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        for key, value in pending.items():
            if value is None:
                continue
            lines.append(f"{key}={value}")
    text = "\n".join(lines).rstrip("\n")
    return f"{text}\n" if text else ""


def save_settings(
    *,
    env_updates: dict[str, Any] | None = None,
    defaults: dict[str, Any] | None = None,
    path: Path | None = None,
) -> AppSettings:
    """写入 .env 并返回重新加载后的配置。

    - `env_updates` 只接受 SECRET_KEYS / CONFIG_KEYS；空串表示清除该键。
    - `defaults` 只接受 DEFAULT_KEY_MAP 的短名（language、proofread 等）。
    - 未知键抛 ValueError，避免误写文件。
    """
    target = path or env_path()
    updates: dict[str, str | None] = {}
    for key, value in (env_updates or {}).items():
        if key not in ALLOWED_ENV_KEYS:
            raise ValueError(f"不允许写入的配置键: {key}")
        text = "" if value is None else str(value).strip()
        updates[key] = text or None

    for name, value in (defaults or {}).items():
        env_key = f"PDF2TEX_{name.upper()}"
        if env_key not in DEFAULT_KEY_MAP:
            raise ValueError(f"不允许写入的默认参数: {name}")
        if isinstance(value, bool):
            updates[env_key] = "1" if value else "0"
        else:
            updates[env_key] = str(value).strip() or None

    existing = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    if not existing:
        existing = [FILE_HEADER.rstrip("\n")]
    rendered = _render_env(existing, updates)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(rendered, encoding="utf-8", newline="\n")
    tmp.replace(target)
    return load_settings(target)

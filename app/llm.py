"""OpenAI-compatible chat client with conservative error handling."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from app.contracts import LLMSettings


class LLMError(RuntimeError):
    """Raised when an LLM request or JSON response cannot be used."""


_JSON_DECODER = json.JSONDecoder()
_JSON_START = re.compile(r"[\[{\"tfn-]|-?\d")


def _without_secrets(message: str, *secrets: str) -> str:
    text = message.replace("\x00", "")
    for secret in secrets:
        value = str(secret)
        if value:
            text = text.replace(value, "[redacted]")
    return text.strip() or "LLM 请求失败"


def _extract_json_value(raw: str) -> Any:
    """Extract the first complete JSON value, including fenced JSON."""
    text = raw.strip()
    fence = re.match(r"^```(?:json|JSON)?\s*\n(.*)\n?```\s*$", text, re.S)
    if fence:
        text = fence.group(1).strip()

    index = 0
    while index < len(text):
        if not _JSON_START.match(text, index):
            index += 1
            continue
        try:
            value, end = _JSON_DECODER.raw_decode(text, index)
        except (json.JSONDecodeError, ValueError):
            index += 1
            continue
        return value
    raise ValueError("no JSON value found")


class LLMClient:
    """A small adapter around OpenAI's chat-completions API."""

    def __init__(self, settings: LLMSettings) -> None:
        self.api_key = str(settings.api_key)
        self.base_url = str(settings.base_url)
        self.model = str(settings.model)
        self.timeout = float(settings.timeout)
        try:
            self.max_retries = max(0, int(settings.max_retries))
        except (TypeError, ValueError):
            self.max_retries = 0

        self._settings = settings
        try:
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url or None,
                timeout=self.timeout,
                max_retries=0,
            )
        except Exception as exc:
            raise LLMError(_without_secrets(f"无法初始化 LLM 客户端: {exc}", self.api_key)) from exc

    def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        """Return the first chat response, retrying transient transport errors."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        extra_body = getattr(self._settings, "extra_body", None)
        if isinstance(extra_body, dict) and extra_body:
            payload["extra_body"] = dict(extra_body)

        attempts = self.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._client.chat.completions.create(**payload)
                choices = getattr(response, "choices", None)
                if not choices:
                    raise ValueError("LLM 响应缺少 choices")
                content = getattr(choices[0].message, "content", None)
                if not isinstance(content, str):
                    raise ValueError("LLM 响应缺少文本内容")
                return content
            except APITimeoutError as exc:
                last_error = exc
            except APIConnectionError as exc:
                last_error = exc
            except APIStatusError as exc:
                status = getattr(exc, "status_code", None)
                if isinstance(status, int) and 500 <= status < 600:
                    last_error = exc
                else:
                    raise LLMError(
                        _without_secrets(f"LLM 请求被拒绝: {exc}", self.api_key)
                    ) from exc
            except Exception as exc:
                raise LLMError(
                    _without_secrets(f"LLM 请求失败: {exc}", self.api_key)
                ) from exc

            if attempt + 1 < attempts:
                time.sleep(min(8.0, 0.5 * (2**attempt)))

        raise LLMError(
            _without_secrets(
                f"LLM 请求在重试 {self.max_retries} 次后仍失败: {last_error}",
                self.api_key,
            )
        ) from last_error

    def chat_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float = 0.0,
    ) -> Any:
        raw = self.chat(messages, temperature=temperature)
        try:
            return _extract_json_value(raw)
        except Exception as exc:
            raise LLMError("LLM 响应不是有效的 JSON") from exc


def build_client(settings: LLMSettings | None) -> LLMClient | None:
    if settings is None or not settings.enabled:
        return None
    return LLMClient(settings)

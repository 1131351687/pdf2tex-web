"""离线自测用的假 MinerU V4 服务与假 OpenAI 兼容服务。

用途：在没有真实 API key 的情况下跑通整条流水线（OCR → 修复 → pandoc →
XeLaTeX），也方便手工在浏览器里试用界面。

直接运行会同时启动两个服务：

    python tools/mock_services.py --mineru-port 8990 --llm-port 8991

也可以被测试或脚本 import：

    from tools.mock_services import start_servers
    servers = start_servers()
    ...
    servers.shutdown()
"""

from __future__ import annotations

import argparse
import io
import json
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MOCK_MARKDOWN = """# 示例文档：微分中值定理

本文用于验证 pdf2tex-web 的离线流程，内容包含中文、行内公式、行间公式与表格。

## 1. 定义

设函数 $f(x)$ 在闭区间 $[a, b]$ 上连续，在开区间 $(a, b)$ 内可导，则存在
$\\xi \\in (a, b)$ 使得

$$f'(\\xi) = \\frac{f(b) - f(a)}{b - a}$$

## 2. 常见迭代法比较

| 方法 | 收敛阶 | 说明 |
| --- | --- | --- |
| 二分法 | $O(\\log n)$ | 稳健，但收敛较慢 |
| 牛顿法 | $O(n^2)$ | 初值需要足够接近根 |

## 3. 特例

当 $f(a) = f(b)$ 时，上述结论退化为罗尔定理。
"""

MOCK_CONTENT_LIST = [
    {"type": "text", "page_idx": 0, "text": "示例文档", "text_level": 1},
    {"type": "text", "page_idx": 1, "text": "常见迭代法比较"},
    {"type": "text", "page_idx": 2, "text": "特例"},
]


def build_result_zip() -> bytes:
    """构造 MinerU V4 风格的产物压缩包（不含 full.tex，走 Markdown 路线）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("full.md", MOCK_MARKDOWN)
        archive.writestr(
            "content_list.json",
            json.dumps(MOCK_CONTENT_LIST, ensure_ascii=False, indent=2),
        )
        archive.writestr("images/.keep", "")
    return buffer.getvalue()


class _BatchState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.counter = 0
        self.uploads: dict[str, int] = {}

    def new_batch(self) -> str:
        with self.lock:
            self.counter += 1
            return f"mock-batch-{self.counter:04d}"


class MockMinerUHandler(BaseHTTPRequestHandler):
    server_version = "MockMinerU/0.1"
    state: _BatchState = _BatchState()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 保持静默
        return

    # ------------------------------------------------------------------ 工具
    def _base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ 路由
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path == "/api/v4/file-urls/batch":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            batch_id = self.state.new_batch()
            self._send_json(
                {
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": batch_id,
                        "file_urls": [f"{self._base_url()}/upload/{batch_id}"],
                    },
                }
            )
            return
        self._send_json({"code": 404, "msg": f"unknown path {self.path}"}, status=404)

    def do_PUT(self) -> None:  # noqa: N802
        match = re.fullmatch(r"/upload/([\w-]+)", self.path)
        if not match:
            self._send_json({"code": 404, "msg": "unknown upload path"}, status=404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else b""
        if not payload:
            self._send_json({"code": 400, "msg": "empty upload"}, status=400)
            return
        with self.state.lock:
            self.state.uploads[match.group(1)] = len(payload)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        batch = re.fullmatch(r"/api/v4/extract-results/batch/([\w-]+)", self.path)
        if batch:
            batch_id = batch.group(1)
            with self.state.lock:
                size = self.state.uploads.get(batch_id)
            if size is None:
                self._send_json({"code": 0, "data": {"extract_result": []}})
                return
            self._send_json(
                {
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": batch_id,
                        "extract_result": [
                            {
                                "file_name": "input.pdf",
                                "state": "done",
                                "full_zip_url": f"{self._base_url()}/result/{batch_id}.zip",
                                "err_msg": "",
                            }
                        ],
                    },
                }
            )
            return

        archive = re.fullmatch(r"/result/([\w-]+)\.zip", self.path)
        if archive:
            self._send_bytes(build_result_zip(), "application/zip")
            return

        if self.path == "/health":
            self._send_json({"ok": True, "service": "mock-mineru"})
            return

        self._send_json({"code": 404, "msg": f"unknown path {self.path}"}, status=404)


def _extract_markdown(prompt: str) -> str | None:
    """从提示词里取出 ```markdown 围栏内容；取不到则取最长的任意围栏。"""
    labelled = re.search(r"```markdown\s*\n(.*?)```", prompt, re.S)
    if labelled:
        return labelled.group(1)
    blocks = re.findall(r"```[^\n]*\n(.*?)```", prompt, re.S)
    if not blocks:
        return None
    return max(blocks, key=len)


class MockLLMHandler(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容实现：/v1/chat/completions 与 /v1/models。"""

    server_version = "MockLLM/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._send_json(
                {
                    "object": "list",
                    "data": [{"id": "mock-model", "object": "model", "owned_by": "mock"}],
                }
            )
            return
        self._send_json({"error": {"message": "not found"}}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json({"error": {"message": "not found"}}, status=404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._send_json({"error": {"message": "invalid json"}}, status=400)
            return

        messages = payload.get("messages") or []
        system = " ".join(
            str(item.get("content", "")) for item in messages if item.get("role") == "system"
        )
        last_user = ""
        for item in reversed(messages):
            if item.get("role") == "user":
                last_user = str(item.get("content", ""))
                break
        combined = f"{system}\n{last_user}"

        if '"patches"' in combined or "patches" in system:
            content = json.dumps({"patches": [], "reason": "mock: 无需补丁"}, ensure_ascii=False)
        else:
            content = _extract_markdown(last_user) or last_user

        self._send_json(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": 0,
                "model": payload.get("model", "mock-model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )


class Servers:
    def __init__(self, mineru_port: int, llm_port: int) -> None:
        self.mineru = ThreadingHTTPServer(("127.0.0.1", mineru_port), MockMinerUHandler)
        self.llm = ThreadingHTTPServer(("127.0.0.1", llm_port), MockLLMHandler)
        self._threads = [
            threading.Thread(target=self.mineru.serve_forever, daemon=True),
            threading.Thread(target=self.llm.serve_forever, daemon=True),
        ]

    @property
    def mineru_url(self) -> str:
        host, port = self.mineru.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def llm_url(self) -> str:
        host, port = self.llm.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> "Servers":
        for thread in self._threads:
            thread.start()
        return self

    def shutdown(self) -> None:
        for server in (self.mineru, self.llm):
            server.shutdown()
            server.server_close()


def start_servers(mineru_port: int = 0, llm_port: int = 0) -> Servers:
    """启动两个本地假服务（端口传 0 表示自动分配）。"""
    return Servers(mineru_port, llm_port).start()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mineru-port", type=int, default=8990)
    parser.add_argument("--llm-port", type=int, default=8991)
    args = parser.parse_args()
    servers = start_servers(args.mineru_port, args.llm_port)
    print(f"假 MinerU 服务: {servers.mineru_url}")
    print(f"假 LLM 服务:  {servers.llm_url}")
    print("按 Ctrl+C 结束。")
    try:
        for thread in servers._threads:
            thread.join()
    except KeyboardInterrupt:
        print("\n正在关闭 ...")
    finally:
        servers.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Reproducible MinerU client for Agent and V4 benchmark runs.

The API token is read from MINERU_API_TOKEN. State is persisted after every
remote transition so interrupted runs can resume without uploading again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

import requests

from finalize_mineru_tex import finalize


BASE_URL = "https://mineru.net"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def safe_extract(archive: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    root = output_dir.resolve()
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            target = (output_dir / member.filename).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"Unsafe ZIP member: {member.filename}")
        zipped.extractall(output_dir)


class MinerUClient:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        # Local HTTP proxies frequently break MinerU's OSS/CDN TLS handshake.
        self.session.trust_env = False
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def json_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.request(method, url, timeout=60, **kwargs)
        if not response.ok:
            body = response.text[:500].replace("\n", " ")
            raise RuntimeError(f"HTTP {response.status_code} from {url}: {body}")
        payload = response.json()
        if isinstance(payload, dict) and payload.get("code") not in (None, 0):
            raise RuntimeError(f"MinerU error: {payload.get('msg', payload)}")
        return payload

    def upload(self, url: str, pdf: Path) -> None:
        with pdf.open("rb") as stream:
            response = self.session.put(url, data=stream, timeout=600)
        if response.status_code not in (200, 201, 204):
            raise RuntimeError(f"Upload failed: HTTP {response.status_code}")

    def download(self, url: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        with self.session.get(url, stream=True, timeout=300) as response:
            response.raise_for_status()
            with tmp.open("wb") as stream:
                shutil.copyfileobj(response.raw, stream)
        tmp.replace(target)


def initial_state(pdf: Path, mode: str, language: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "mode": mode,
        "language": language,
        "pdf": str(pdf.resolve()),
        "sha256": file_sha256(pdf),
        "bytes": pdf.stat().st_size,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "new",
        "timings": {},
    }


def load_compatible_state(
    state_path: Path, pdf: Path, mode: str, language: str
) -> dict[str, Any]:
    state = load_json(state_path) or initial_state(pdf, mode, language)
    if state["sha256"] != file_sha256(pdf):
        raise RuntimeError("State file belongs to a different PDF")
    recorded_language = state.get("language")
    if recorded_language and recorded_language != language:
        raise RuntimeError(
            f"State file uses language={recorded_language!r}, not {language!r}"
        )
    state["language"] = language
    return state


def run_agent(
    client: MinerUClient,
    pdf: Path,
    out: Path,
    state_path: Path,
    language: str,
) -> dict[str, Any]:
    state = load_compatible_state(state_path, pdf, "agent", language)

    if not state.get("task_id"):
        started = time.perf_counter()
        payload = client.json_request(
            "POST",
            f"{BASE_URL}/api/v1/agent/parse/file",
            json={"url": "", "file_name": pdf.name},
        )
        data = payload["data"]
        client.upload(data["file_url"], pdf)
        state.update(task_id=data["task_id"], status="uploaded")
        state["timings"]["submit_upload_seconds"] = round(time.perf_counter() - started, 3)
        atomic_json(state_path, state)

    poll_started = time.perf_counter()
    for attempt in range(180):
        payload = client.json_request(
            "GET", f"{BASE_URL}/api/v1/agent/parse/{state['task_id']}"
        )
        data = payload.get("data", {})
        status = data.get("state", "unknown")
        state.update(status=status, poll_attempts=attempt + 1)
        atomic_json(state_path, state)
        print(f"agent: {status} ({attempt + 1})", flush=True)
        if status == "done":
            started = time.perf_counter()
            target = out / "result.md"
            client.download(data["markdown_url"], target)
            state["markdown"] = str(target.resolve())
            state["timings"]["download_seconds"] = round(time.perf_counter() - started, 3)
            break
        if status == "failed":
            raise RuntimeError(data.get("err_msg", "Agent parse failed"))
        time.sleep(5)
    else:
        raise TimeoutError("Agent parse timed out")
    state["timings"]["poll_seconds"] = round(time.perf_counter() - poll_started, 3)
    atomic_json(state_path, state)
    return state


def run_v4(
    client: MinerUClient,
    pdf: Path,
    out: Path,
    state_path: Path,
    language: str,
) -> dict[str, Any]:
    state = load_compatible_state(state_path, pdf, "v4-vlm", language)

    if not state.get("batch_id"):
        started = time.perf_counter()
        data_id = state["sha256"][:16]
        payload = client.json_request(
            "POST",
            f"{BASE_URL}/api/v4/file-urls/batch",
            json={
                "files": [{"name": pdf.name, "data_id": data_id}],
                "model_version": "vlm",
                "language": language,
                "enable_formula": True,
                "enable_table": True,
                "extra_formats": ["latex"],
            },
        )
        data = payload["data"]
        client.upload(data["file_urls"][0], pdf)
        state.update(batch_id=data["batch_id"], data_id=data_id, status="uploaded")
        state["timings"]["submit_upload_seconds"] = round(time.perf_counter() - started, 3)
        atomic_json(state_path, state)

    poll_started = time.perf_counter()
    for attempt in range(240):
        payload = client.json_request(
            "GET", f"{BASE_URL}/api/v4/extract-results/batch/{state['batch_id']}"
        )
        items = payload.get("data", {}).get("extract_result", [])
        item = items[0] if items else {}
        status = item.get("state", "queued")
        state.update(status=status, poll_attempts=attempt + 1)
        atomic_json(state_path, state)
        print(f"v4-vlm: {status} ({attempt + 1})", flush=True)
        if status == "done":
            zip_url = item.get("full_zip_url") or payload.get("data", {}).get("full_zip_url")
            if not zip_url:
                raise RuntimeError("V4 completed without full_zip_url")
            started = time.perf_counter()
            archive = out / "result.zip"
            client.download(zip_url, archive)
            result_dir = out / "result"
            safe_extract(archive, result_dir)
            generated_tex = result_dir / "full.tex"
            if generated_tex.exists():
                fixed_tex = result_dir / "full.fixed.tex"
                fixed, changes = finalize(generated_tex.read_text(encoding="utf-8"))
                fixed_tex.write_text(fixed, encoding="utf-8", newline="\n")
                state["fixed_tex"] = str(fixed_tex.resolve())
                state["tex_fixes"] = changes
            state["archive"] = str(archive.resolve())
            state["timings"]["download_extract_seconds"] = round(time.perf_counter() - started, 3)
            break
        if status == "failed":
            raise RuntimeError(item.get("err_msg", "V4 parse failed"))
        time.sleep(5)
    else:
        raise TimeoutError("V4 parse timed out")
    state["timings"]["poll_seconds"] = round(time.perf_counter() - poll_started, 3)
    atomic_json(state_path, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("agent", "v4"))
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--language",
        default=os.environ.get("MINERU_LANGUAGE", "ch"),
        help="OCR language code (default: MINERU_LANGUAGE or ch)",
    )
    args = parser.parse_args()
    language = args.language.strip() or "ch"
    token = os.environ.get("MINERU_API_TOKEN", "").strip()
    if not token:
        parser.error("set MINERU_API_TOKEN; tokens must not be stored in source code")
    pdf = args.pdf.resolve()
    if not pdf.is_file():
        parser.error(f"PDF not found: {pdf}")
    args.output.mkdir(parents=True, exist_ok=True)
    state_path = args.output / "state.json"
    client = MinerUClient(token)
    if args.mode == "agent":
        run_agent(client, pdf, args.output, state_path, language)
    else:
        run_v4(client, pdf, args.output, state_path, language)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

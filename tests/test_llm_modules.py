"""Offline tests for the LLM client, Markdown proofreader, and repair loop."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import llm as llm_module
from app import repair_loop as repair_loop_module
from app.contracts import LLMSettings
from app.llm import LLMClient, LLMError, build_client
from app.proofread import proofread_markdown, split_markdown
from app.repair_loop import compile_with_repair


def make_client(content: str | Exception, monkeypatch=None, calls: list[dict] | None = None):
    class FakeCompletions:
        def create(self, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            if isinstance(content, Exception):
                raise content
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    if monkeypatch is not None:
        monkeypatch.setattr(llm_module, "OpenAI", FakeOpenAI)
    settings = LLMSettings(api_key="secret-key", base_url="http://localhost", model="test")
    return LLMClient(settings)


def test_chat_json_accepts_plain_json_and_fences(monkeypatch):
    cases = [
        '{"ok": true}',
        "```json\n{\"ok\": true}\n```",
        "Here is the result:\n```json\n{\"ok\": true}\n```\nDone.",
        "prefix {\"ok\": true} suffix",
    ]
    for raw in cases:
        client = make_client(raw, monkeypatch)
        assert client.chat_json([]) == {"ok": True}


def test_chat_json_rejects_invalid_json(monkeypatch):
    client = make_client("This is not JSON.", monkeypatch)
    with pytest.raises(LLMError):
        client.chat_json([])


def test_chat_error_is_wrapped_without_api_key(monkeypatch):
    client = make_client(RuntimeError("connection failed secret-key"), monkeypatch)
    with pytest.raises(LLMError) as exc_info:
        client.chat([{"role": "user", "content": "hi"}])
    assert "secret-key" not in str(exc_info.value)


def test_build_client_disabled():
    assert build_client(None) is None
    assert build_client(LLMSettings(api_key="", base_url="http://localhost")) is None


def test_split_markdown_prefers_headings_and_restores_text():
    text = "# First\nalpha beta\n## Second\ngamma\ntext"
    chunks = split_markdown(text, max_chars=12, max_chunks=10)
    assert chunks[0].start == 0
    assert chunks[0].text.startswith("# First")
    assert all(len(chunk.text) <= 12 for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == text


def test_split_markdown_allows_long_atomic_block():
    text = "```python\n" + ("x = 1\n" * 100) + "```"
    chunks = split_markdown(text, max_chars=20, max_chunks=10)
    assert len(chunks) == 1
    assert chunks[0].text == text


def test_split_markdown_too_many_chunks_raises_chinese_error():
    text = "# A\nalpha\n# B\nbeta"
    with pytest.raises(ValueError, match="文档太大或建议关闭校对"):
        split_markdown(text, max_chars=8, max_chunks=1)


def test_proofread_applies_valid_revisions():
    class Client:
        def chat(self, messages, **kwargs):
            assert "```markdown" in messages[-1]["content"]
            if "bad" in messages[-1]["content"]:
                return "# A\ngood"
            return "# B\nokay"

    text = "# A\nbad\n# B\nokay"
    result = proofread_markdown(text, Client(), max_chars=20)
    assert result.markdown == "# A\ngood# B\nokay"
    assert (result.total_chunks, result.applied, result.skipped) == (2, 2, 0)


def test_proofread_discards_invalid_revision():
    class Client:
        def chat(self, messages, **kwargs):
            return "x"

    result = proofread_markdown("# A\nbad", Client(), max_chars=20)
    assert result.markdown == "# A\nbad"
    assert result.applied == 0
    assert result.skipped == 1
    assert result.warnings


def test_proofread_discards_changed_image_reference():
    class Client:
        def chat(self, messages, **kwargs):
            return "# A\n![new](images/new.png)"

    text = "# A\n![old](images/old.png)"
    result = proofread_markdown(text, Client(), max_chars=100)
    assert result.markdown == text
    assert result.skipped == 1


def test_proofread_retains_chunk_on_llm_error():
    class Client:
        def chat(self, messages, **kwargs):
            raise LLMError("temporary failure")

    text = "# A\nbad"
    result = proofread_markdown(text, Client(), max_chars=20)
    assert result.markdown == text
    assert result.skipped == 1
    assert any("temporary failure" in warning for warning in result.warnings)


def test_repair_loop_fixes_then_passes(tmp_path, monkeypatch):
    tex = tmp_path / "book.tex"
    tex.write_text("line1\nBAD\nline3\n", encoding="utf-8")
    calls: list[dict] = []

    class Client:
        def chat_json(self, messages, **kwargs):
            assert "1: line1" in messages[-1]["content"]
            return {
                "patches": [{"find": "BAD", "replace": "GOOD"}],
                "reason": "fix token",
            }

    reports = [
        {"passed": False, "errors": ["! Undefined control sequence. l.2 BAD"]},
        {"passed": True, "errors": []},
    ]

    def fake_compile_tex(tex_path: Path, work_dir: Path):
        assert reports
        report = reports.pop(0)
        if not report["passed"] and "BAD" in tex_path.read_text(encoding="utf-8"):
            report = {"passed": False, "errors": ["! Undefined control sequence. l.2 BAD"]}
        else:
            report = {"passed": True, "errors": []}
        return report, work_dir / "book.log"

    monkeypatch.setattr(repair_loop_module, "compile_tex", fake_compile_tex)
    calls.clear()
    outcome = compile_with_repair(tex, tmp_path, Client(), rounds=2)
    assert outcome.passed is True
    assert outcome.rounds_used == 1
    assert tex.read_text(encoding="utf-8") == "line1\nGOOD\nline3\n"
    assert (tmp_path / "backup-r1.tex").read_text(encoding="utf-8") == "line1\nBAD\nline3\n"
    assert calls == []


def test_repair_loop_always_failing_uses_all_rounds(tmp_path, monkeypatch):
    tex = tmp_path / "book.tex"
    tex.write_text("BAD\n", encoding="utf-8")

    class Client:
        def __init__(self):
            self.count = 0

        def chat_json(self, messages, **kwargs):
            self.count += 1
            return {
                "patches": [{"find": "BAD", "replace": f"BAD-{self.count}"}],
                "reason": "attempt",
            }

    def fake_compile_tex(tex_path: Path, work_dir: Path):
        return {"passed": False, "errors": ["! Compilation failed"]}, work_dir / "book.log"

    monkeypatch.setattr(repair_loop_module, "compile_tex", fake_compile_tex)
    client = Client()
    outcome = compile_with_repair(tex, tmp_path, client, rounds=3)
    assert outcome.passed is False
    assert outcome.rounds_used == 3
    assert client.count == 3
    assert len(outcome.warnings) == 3
    for number in (1, 2, 3):
        assert (tmp_path / f"backup-r{number}.tex").exists()


def test_repair_loop_skips_non_unique_find(tmp_path, monkeypatch):
    tex = tmp_path / "book.tex"
    original = "same\nsame\n"
    tex.write_text(original, encoding="utf-8")

    class Client:
        def chat_json(self, messages, **kwargs):
            return {"patches": [{"find": "same", "replace": "unique"}], "reason": "ambiguous"}

    compile_count = 0

    def fake_compile_tex(tex_path: Path, work_dir: Path):
        nonlocal compile_count
        compile_count += 1
        return {"passed": False, "errors": ["! Broken"]}, work_dir / "book.log"

    monkeypatch.setattr(repair_loop_module, "compile_tex", fake_compile_tex)
    outcome = compile_with_repair(tex, tmp_path, Client(), rounds=1)
    assert outcome.passed is False
    assert outcome.rounds_used == 1
    assert compile_count == 2
    assert tex.read_text(encoding="utf-8") == original
    assert not (tmp_path / "backup-r1.tex").exists()
    assert any("find 不唯一" in warning for warning in outcome.warnings)


def test_json_payload_shape_is_reported():
    assert json.dumps({"patches": [], "reason": ""})

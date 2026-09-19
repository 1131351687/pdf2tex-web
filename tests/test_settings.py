"""配置读写单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.settings import load_settings, read_env_file, save_settings


@pytest.fixture()
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / ".env"
    monkeypatch.setenv("PDF2TEX_ENV_PATH", str(target))
    for key in (
        "MINERU_API_TOKEN",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "PDF2TEX_LANGUAGE",
        "PDF2TEX_PROOFREAD",
        "PDF2TEX_LLM_REPAIR",
        "PDF2TEX_REPAIR_ROUNDS",
        "PDF2TEX_CHUNK_SIZE",
    ):
        monkeypatch.delenv(key, raising=False)
    return target


def test_save_and_reload(env_file: Path) -> None:
    saved = save_settings(
        env_updates={"MINERU_API_TOKEN": "mineru-secret-abcd", "LLM_API_KEY": "sk-secret-wxyz"},
        defaults={"language": "en", "proofread": True, "repair_rounds": 3},
    )
    assert saved.mineru_token == "mineru-secret-abcd"
    assert saved.llm.api_key == "sk-secret-wxyz"
    assert saved.defaults.language == "en"
    assert saved.defaults.proofread is True
    assert saved.defaults.repair_rounds == 3

    reloaded = load_settings()
    assert reloaded.defaults.proofread is True
    assert reloaded.defaults.language == "en"

    raw = env_file.read_text(encoding="utf-8")
    assert "MINERU_API_TOKEN=mineru-secret-abcd" in raw


def test_public_dict_masks_secrets(env_file: Path) -> None:
    saved = save_settings(
        env_updates={"MINERU_API_TOKEN": "abcdefgh1234", "LLM_API_KEY": "sk-12345678"}
    )
    public = saved.public_dict()
    assert public["mineru_token"]["set"] is True
    assert public["mineru_token"]["mask"].endswith("1234")
    assert "abcdefgh1234" not in str(public)
    assert "sk-12345678" not in str(public)
    assert public["llm"]["api_key"]["set"] is True


def test_empty_value_clears_key(env_file: Path) -> None:
    save_settings(env_updates={"MINERU_API_TOKEN": "temp-token-1234"})
    save_settings(env_updates={"MINERU_API_TOKEN": ""})
    assert read_env_file(env_file).get("MINERU_API_TOKEN") is None
    assert load_settings().mineru_token == ""


def test_unknown_keys_rejected(env_file: Path) -> None:
    with pytest.raises(ValueError):
        save_settings(env_updates={"PATH": "c:/windows"})
    with pytest.raises(ValueError):
        save_settings(defaults={"proof_read": True})


def test_env_var_fallback_when_file_missing(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINERU_API_TOKEN", "from-env-token")
    assert not env_file.exists()
    assert load_settings().mineru_token == "from-env-token"


def test_existing_comments_preserved(env_file: Path) -> None:
    env_file.write_text(
        "# 我的注释\nLLM_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )
    save_settings(env_updates={"LLM_API_KEY": "sk-abcdefgh"})
    raw = env_file.read_text(encoding="utf-8")
    assert "# 我的注释" in raw
    assert "LLM_MODEL=deepseek-chat" in raw
    assert "LLM_API_KEY=sk-abcdefgh" in raw

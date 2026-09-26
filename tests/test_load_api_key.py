"""_load_api_key precedence: env var, profile-local secret, then root secret.

conftest's hermetic fixture replaces ``music._load_api_key`` with a stub, so the
real function reference is captured at import time (before fixtures run).
"""
import os

import pytest

from plugins.voice_stack import music

_REAL_LOAD_API_KEY = music._load_api_key  # captured before conftest patches it


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "music"))


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"TYPESAFE_API_KEY={value}\n", encoding="utf-8")


def test_env_var_wins_over_files(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    _write(tmp_path / "profiles" / "music" / "secrets" / "typesafe.env", "local-key")
    assert _REAL_LOAD_API_KEY() == "env-key"


def test_profile_local_file_wins_over_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _write(tmp_path / "profiles" / "music" / "secrets" / "typesafe.env", "local-key")
    _write(tmp_path / "home" / ".hermes" / "secrets" / "typesafe.env", "root-key")
    assert _REAL_LOAD_API_KEY() == "local-key"


def test_falls_back_to_root_when_no_local(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _write(tmp_path / "home" / ".hermes" / "secrets" / "typesafe.env", "root-key")
    assert _REAL_LOAD_API_KEY() == "root-key"


def test_empty_when_nothing_present(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "emptyhome"))
    assert _REAL_LOAD_API_KEY() == ""


def test_missing_local_skipped_for_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _write(tmp_path / "home" / ".hermes" / "secrets" / "typesafe.env", "root-key")
    assert _REAL_LOAD_API_KEY() == "root-key"
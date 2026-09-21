"""Hermetic defaults: tests must opt into fakes, never production services."""
import socket
import pytest


@pytest.fixture(autouse=True)
def hermetic_runtime(monkeypatch, tmp_path):
    from plugins.voice_stack import events, music
    monkeypatch.setenv("HERMES_VOICE_EVENT_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(events, "DEFAULT_EVENT_LOG", tmp_path / "default-events.jsonl")
    def blocked(*args, **kwargs):
        raise RuntimeError("Hermetic test blocked real network/device access")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(music, "_load_api_key", lambda: "")
    # NB: do NOT stub duckdb/postgres/hotwords here — test_music's own
    # `_isolate_artist_db` fixture covers those, and stubbing unconditionally
    # breaks tests that exercise the real candidate ranking.

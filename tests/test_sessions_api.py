"""Tests for the Hermes Sessions API client used by the voice fallback path."""

from __future__ import annotations

import json

import pytest

from plugins.voice_stack import sessions_api


class FakeResponse:
    """Minimal stand-in for a streamed ``requests`` response."""

    def __init__(self, lines, status_code=200, text=""):
        self._lines = lines
        self.status_code = status_code
        self.text = text
        self.encoding = "latin-1"
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        yield from self._lines

    def close(self):
        self.closed = True


def sse(event: str, payload: dict) -> list[str]:
    return [f"event: {event}", f"data: {json.dumps(payload)}", ""]


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "test-key")
    return "test-key"


class TestAuth:
    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("API_SERVER_KEY", raising=False)
        monkeypatch.delenv("HERMES_API_KEY", raising=False)
        with pytest.raises(sessions_api.SessionsAPIUnavailable, match="API_SERVER_KEY"):
            sessions_api._headers()

    def test_falls_back_to_hermes_api_key(self, monkeypatch):
        monkeypatch.delenv("API_SERVER_KEY", raising=False)
        monkeypatch.setenv("HERMES_API_KEY", "alt-key")
        assert sessions_api._headers()["Authorization"] == "Bearer alt-key"


class TestEnsureSession:
    def test_conflict_means_already_created(self, api_key, monkeypatch):
        import requests

        monkeypatch.setattr(
            requests, "post", lambda *a, **k: FakeResponse([], status_code=409)
        )
        assert sessions_api.ensure_session("voice-stack") == "voice-stack"

    def test_server_error_raises(self, api_key, monkeypatch):
        import requests

        monkeypatch.setattr(
            requests, "post", lambda *a, **k: FakeResponse([], status_code=500, text="boom")
        )
        with pytest.raises(sessions_api.SessionsAPIUnavailable, match="session create failed"):
            sessions_api.ensure_session("voice-stack")

    def test_profile_session_uses_profile_api_url(self, api_key, monkeypatch):
        import requests

        calls = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return FakeResponse([], status_code=409)

        monkeypatch.setenv("HERMES_SPORTSCOACH_API_BASE_URL", "http://127.0.0.1:8643/")
        monkeypatch.setattr(requests, "post", fake_post)

        assert sessions_api.ensure_session("voice-stack", profile="sportscoach") == "voice-stack"
        assert calls == ["http://127.0.0.1:8643/api/sessions"]


class TestStreamParsing:
    def _run(self, monkeypatch, api_key, lines, **kwargs):
        import requests

        response = FakeResponse(lines)
        monkeypatch.setattr(sessions_api, "ensure_session", lambda *a, **k: "voice-stack")
        monkeypatch.setattr(requests, "post", lambda *a, **k: response)
        return sessions_api.complete("hello", **kwargs), response

    def test_prefers_assistant_completed_content(self, api_key, monkeypatch):
        lines = [
            *sse("run.started", {"run_id": "run_1"}),
            *sse("assistant.delta", {"delta": "Lis"}),
            *sse("assistant.delta", {"delta": "bon"}),
            *sse("assistant.completed", {"content": "The capital is Lisbon."}),
            *sse("run.completed", {"usage": {"input_tokens": 10, "output_tokens": 4}}),
        ]
        answer, _ = self._run(monkeypatch, api_key, lines)
        assert answer == "The capital is Lisbon."

    def test_falls_back_to_concatenated_deltas(self, api_key, monkeypatch):
        lines = [
            *sse("assistant.delta", {"delta": "Lis"}),
            *sse("assistant.delta", {"delta": "bon."}),
            *sse("assistant.completed", {"content": ""}),
        ]
        answer, _ = self._run(monkeypatch, api_key, lines)
        assert answer == "Lisbon."

    def test_forces_utf8_and_closes_response(self, api_key, monkeypatch):
        """SSE carries no charset; requests would guess latin-1 and mangle it."""
        lines = [*sse("assistant.completed", {"content": "Café ouvert."})]
        answer, response = self._run(monkeypatch, api_key, lines)
        assert answer == "Café ouvert."
        assert response.encoding == "utf-8"
        assert response.closed is True

    def test_reports_tool_lifecycle_and_skips_internal_tools(self, api_key, monkeypatch):
        lines = [
            *sse("tool.started", {"tool_name": "web_search", "preview": "marathon record"}),
            *sse("tool.completed", {"tool_name": "web_search"}),
            *sse("tool.started", {"tool_name": "_thinking", "preview": "hmm"}),
            *sse("assistant.completed", {"content": "Done."}),
        ]
        seen = []
        answer, _ = self._run(
            monkeypatch, api_key, lines,
            on_tool=lambda phase, name, payload: seen.append((phase, name)),
        )
        assert answer == "Done."
        assert seen == [("started", "web_search"), ("completed", "web_search")]

    def test_malformed_data_lines_are_skipped(self, api_key, monkeypatch):
        lines = [
            "event: assistant.delta",
            "data: {not json",
            ": a comment line",
            *sse("assistant.completed", {"content": "Still fine."}),
        ]
        answer, _ = self._run(monkeypatch, api_key, lines)
        assert answer == "Still fine."

    def test_run_failed_raises_and_still_closes(self, api_key, monkeypatch):
        lines = [*sse("run.failed", {"error": "model exploded"})]
        import requests

        response = FakeResponse(lines)
        monkeypatch.setattr(sessions_api, "ensure_session", lambda *a, **k: "voice-stack")
        monkeypatch.setattr(requests, "post", lambda *a, **k: response)

        with pytest.raises(sessions_api.SessionsAPIUnavailable, match="stream error"):
            sessions_api.complete("hello")
        assert response.closed is True

    def test_http_error_surfaces_status(self, api_key, monkeypatch):
        import requests

        monkeypatch.setattr(sessions_api, "ensure_session", lambda *a, **k: "voice-stack")
        monkeypatch.setattr(
            requests, "post", lambda *a, **k: FakeResponse([], status_code=503, text="down")
        )
        with pytest.raises(sessions_api.SessionsAPIUnavailable, match="503"):
            sessions_api.complete("hello")

    def test_profile_chat_uses_profile_api_url(self, api_key, monkeypatch):
        import requests

        calls = []
        response = FakeResponse([*sse("assistant.completed", {"content": "Move, Private."})])

        monkeypatch.setenv("HERMES_SPORTSCOACH_API_BASE_URL", "http://127.0.0.1:8643/")
        monkeypatch.setattr(
            sessions_api,
            "ensure_session",
            lambda *a, **k: "voice-stack",
        )

        def fake_post(url, **kwargs):
            calls.append(url)
            return response

        monkeypatch.setattr(requests, "post", fake_post)

        assert sessions_api.complete("fastest run", profile="sportscoach") == "Move, Private."
        assert calls == ["http://127.0.0.1:8643/api/sessions/voice-stack/chat/stream"]

    def test_named_session_and_title_are_forwarded(self, api_key, monkeypatch):
        import requests

        ensured = []
        response = FakeResponse(
            [*sse("assistant.completed", {"content": "Resolved."})]
        )

        def fake_ensure(name=None, *, profile=None, title=None):
            ensured.append((name, profile, title))
            return name or "voice-stack"

        monkeypatch.setattr(sessions_api, "ensure_session", fake_ensure)
        monkeypatch.setattr(requests, "post", lambda *a, **k: response)

        assert sessions_api.complete(
            "hello",
            session="alexa-hermes",
            session_title="Alexa via Hermes",
        ) == "Resolved."
        assert ensured == [("alexa-hermes", None, "Alexa via Hermes")]


class TestConfiguration:
    def test_base_url_and_session_id_are_overridable(self, monkeypatch):
        monkeypatch.setenv("HERMES_API_BASE_URL", "http://127.0.0.1:9999/")
        monkeypatch.setenv("HERMES_VOICE_SESSION_ID", "custom")
        assert sessions_api.base_url() == "http://127.0.0.1:9999"
        assert sessions_api.session_id() == "custom"

    def test_defaults_are_loopback(self, monkeypatch):
        monkeypatch.delenv("HERMES_API_BASE_URL", raising=False)
        assert sessions_api.base_url() == "http://127.0.0.1:8642"

    def test_sportscoach_has_a_separate_default_api_url(self, monkeypatch):
        monkeypatch.delenv("HERMES_SPORTSCOACH_API_BASE_URL", raising=False)
        assert sessions_api.base_url("sportscoach") == "http://127.0.0.1:8643"

    def test_music_has_a_separate_default_api_url(self, monkeypatch):
        monkeypatch.delenv("HERMES_MUSIC_API_BASE_URL", raising=False)
        assert sessions_api.base_url("music") == "http://127.0.0.1:8644"

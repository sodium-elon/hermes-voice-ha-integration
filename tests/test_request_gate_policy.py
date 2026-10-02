"""Semantic policy contract; no inference or device/network access."""
from types import SimpleNamespace


def test_context_preserves_new_addressed_requests(monkeypatch):
    import pytest
    sdk = pytest.importorskip("typesafe_sdk")
    from plugins.voice_stack import request_gate
    calls = []
    monkeypatch.setattr(request_gate.music, "_load_api_key", lambda: "test-key")

    def judge(self, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(answers={"addressed_request": SimpleNamespace(noul=.95)})

    monkeypatch.setattr(sdk.TypeSafeClient, "system_one", judge)
    assert request_gate.authorize_request(
        "Turn on the kitchen light", previous_request="Play music",
        previous_question="Which artist?",
    )
    question = calls[0]["questions"]["addressed_request"]
    assert "new request" in question.criteria["true"]

"""Running-log relay contract; no model, Garmin, database, or speaker calls."""
import concurrent.futures
import threading
from types import SimpleNamespace

import pytest

import plugins.voice_stack as voice


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(voice, "_ALEXA_PENDING", voice.alexa_confirmation.PendingConfirmation())
    monkeypatch.setattr(voice, "_ALEXA_TURN_LOCK", threading.Lock())
    monkeypatch.setattr(voice, "ALEXA_EXECUTOR", executor)
    monkeypatch.setattr(voice, "ALEXA_AGENT_BUDGET_SECONDS", 1)
    monkeypatch.setattr(voice.events, "emit", lambda *_a, **_k: None)
    monkeypatch.setattr(voice, "_schedule_assist_label", lambda _t: None)
    monkeypatch.setattr(voice, "_resolve_last_called_media_player", lambda: "")
    monkeypatch.setattr(voice, "_jev_route_specialist", lambda _t: pytest.fail("Explicit route must bypass Jev"))
    yield
    executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_running_log_reaches_owner_with_original_date_and_shoe(monkeypatch):
    text = "here is for running coach: 2026-10-04 kaldan leyley 2.0"
    seen = []

    def complete(request, **kwargs):
        seen.append((request, kwargs))
        return "Import verified."

    monkeypatch.setattr(voice, "_run_full_agent", complete)
    result = await voice._handle_assist_query_with_llm(
        object(), {"text": text, "conversation_id": "running-log-request", "language": "en"}
    )
    assert len(seen) == 1
    request, kwargs = seen[0]
    assert request == text
    assert kwargs["profile"] == "sportscoach"
    assert kwargs["session"].startswith(voice.ALEXA_SESSION_ID + "-")
    suffix = kwargs["session"].rsplit("-", 1)[-1]
    assert len(suffix) == 32
    assert kwargs["session_title"] == voice.ALEXA_SESSION_TITLE + " " + suffix
    assert result["profile"] == "sportscoach"
    assert result["conversation_id"] == "running-log-request"
    assert result["text"] == "Import verified."


def test_owner_empty_reply_is_a_deliverable_failure(monkeypatch):
    monkeypatch.setattr(voice.sessions_api, "complete", lambda *_a, **_k: "")
    assert voice._run_full_agent("here is for running coach: 2026-10-04 Qiaodan Leili 2.0",
                                 profile="sportscoach", session=voice.ALEXA_SESSION_ID) == (
        "Sports Coach returned no completion. The run import is not confirmed."
    )


def test_owner_api_failure_returns_deliverable_failure_instead_of_raising(monkeypatch):
    def unavailable(*_a, **_k):
        raise voice.sessions_api.SessionsAPIUnavailable("owner API disconnected")

    monkeypatch.setattr(voice.sessions_api, "complete", unavailable)
    assert voice._run_full_agent("here is for running coach: 2026-10-04 Qiaodan Leili 2.0",
                                 profile="sportscoach", session=voice.ALEXA_SESSION_ID) == (
        "Sports Coach is unavailable right now."
    )


@pytest.mark.asyncio
async def test_delayed_owner_failure_is_delivered_once_after_ack(monkeypatch):
    import threading

    release = threading.Event()
    delivered = threading.Event()
    replies = []

    def slow_failure(*_a, **_k):
        assert release.wait(2)
        raise voice.sessions_api.SessionsAPIUnavailable("owner API disconnected")

    def deliver(future, _target, *_generation):
        replies.append(future.result())
        delivered.set()

    monkeypatch.setattr(voice.sessions_api, "complete", slow_failure)
    monkeypatch.setattr(voice, "ALEXA_AGENT_BUDGET_SECONDS", 0.001)
    monkeypatch.setattr(voice, "_schedule_slow_delivery", deliver)
    try:
        result = await voice._handle_assist_query_with_llm(
            object(), {"text": "here is for running coach: 2026-10-04 Qiaodan Leili 2.0",
                       "conversation_id": "slow-log", "language": "en"}
        )
        assert result["slow"] is True
        assert result["profile"] == "sportscoach"
        assert replies == []
    finally:
        release.set()
    assert delivered.wait(2)
    assert replies == ["Sports Coach is unavailable right now."]


@pytest.mark.asyncio
async def test_sportscoach_api_failure_does_not_use_general_llm(monkeypatch):
    def unavailable(*_a, **_k):
        raise voice.sessions_api.SessionsAPIUnavailable("owner API disconnected")

    async def forbidden(**_kwargs):
        pytest.fail("Sportscoach-owned requests must not fall back to a general LLM")

    monkeypatch.setattr(voice, "_run_full_agent", unavailable)
    result = await voice._handle_assist_query_with_llm(
        SimpleNamespace(llm=SimpleNamespace(acomplete=forbidden)),
        {"text": "here is for running coach: 2026-10-04 Qiaodan Leili 2.0",
         "conversation_id": "running-log-failure", "language": "en"},
    )
    assert result["text"] == "Sports Coach is unavailable right now."
    assert result["profile"] == "sportscoach"
    assert result["conversation_id"] == "running-log-failure"

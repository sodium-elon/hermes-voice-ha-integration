import asyncio
import concurrent.futures
import threading
from types import SimpleNamespace
from pathlib import Path
import pytest
from plugins import voice_stack as voice
from plugins.voice_stack import music_request_limits as limits, alexa_confirmation


def test_exact_canonical_name_needs_no_model(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(limits, "evidence", lambda: (["Stromae"], []))
    monkeypatch.setattr(limits.events, "emit", lambda *a, **k: None)
    monkeypatch.setattr(
        limits, "classify", lambda *a: pytest.fail("exact artist went to model")
    )
    assert (
        limits.prepare("play workout music but not Stromae")["excluded_artist"]
        == "Stromae"
    )


def test_question_answer_keeps_original_request_and_confirms_handoff(monkeypatch):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(voice, "ALEXA_EXECUTOR", executor)
    monkeypatch.setattr(voice, "_ALEXA_TURN_LOCK", threading.Lock())
    monkeypatch.setattr(
        voice, "_ALEXA_PENDING", alexa_confirmation.PendingConfirmation()
    )
    monkeypatch.setattr(limits, "PENDING", limits.PendingLimits())
    monkeypatch.setattr(
        voice, "_resolve_last_called_media_player", lambda: "media_player.echo"
    )
    monkeypatch.setattr(voice, "_route_assist_profile", lambda t: "music")
    monkeypatch.setattr(voice, "_schedule_assist_label", lambda t: None)
    monkeypatch.setattr(voice.events, "emit", lambda *a, **k: None)

    async def keep_open(text):
        return False

    monkeypatch.setattr(voice.conversation_end, "should_end", keep_open)
    monkeypatch.setattr(limits, "evidence", lambda: (["Stromae"], []))
    monkeypatch.setattr(limits, "classify", lambda *a: None)
    calls = []

    def dj(text, **kwargs):
        calls.append(text)
        return "Selection completed."

    monkeypatch.setattr(voice.sessions_api, "complete", dj)

    def ask(text):
        return asyncio.run(
            voice._handle_assist_query_with_llm(
                SimpleNamespace(), {"text": text, "conversation_id": "alexa_dj_yakkuza"}
            )
        )

    try:
        original = "play workout music but not garbled artist"
        question = ask(original)
        assert question["reprompt"] and not calls
        answer = ask("Stromae")
        assert len(calls) == 1 and original in calls[0]
        assert "Excluded artist for this request: Stromae." in calls[0]
        assert answer["text"].startswith(
            "Passed your request to DJ Yakkuza, excluding Stromae."
        )
        assert limits.PENDING.answer("alexa_dj_yakkuza", "Stromae") is None
    finally:
        executor.shutdown(wait=True)


def test_pending_expires_and_can_be_cancelled():
    clock = [0]
    p = limits.PendingLimits(clock=lambda: clock[0])
    p.remember("a", "original")
    assert p.answer("other", "Stromae") is None
    assert p.answer("a", "cancel") == {"cancelled": True}
    p.remember("a", "original")
    clock[0] = 181
    assert p.answer("a", "Stromae") is None

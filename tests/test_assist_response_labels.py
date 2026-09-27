"""Assist reply labels are observations only, never playback proof or speech gates."""
import sys
from types import SimpleNamespace

import pytest

from plugins.voice_stack import events, music


@pytest.fixture
def sdk(monkeypatch):
    captured = []
    answer = SimpleNamespace(choice="playback_claim", confidence=0.9)

    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def system_one(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(answers={"kind": answer})

    monkeypatch.setitem(sys.modules, "typesafe_sdk", SimpleNamespace(Choice=SimpleNamespace, TypeSafeClient=Client))
    monkeypatch.setattr(music, "_load_api_key", lambda: "test-key")
    return captured, answer


def test_jev_labels_playback_claim_without_claiming_audible_playback(sdk):
    from plugins.voice_stack import assist_response_labels
    calls, answer = sdk
    result = assist_response_labels.label_spoken("Playing des fleurs on Apple Music")
    assert result == {"kind": "playback_claim", "confidence": 0.9}
    assert calls[0]["state"] == {"spoken_text": "Playing des fleurs on Apple Music"}
    assert "acceptance" not in calls[0]["questions"]["kind"].criteria


def test_jev_labels_music_content(sdk):
    from plugins.voice_stack import assist_response_labels
    _, answer = sdk
    answer.choice = "music_content"
    assert assist_response_labels.label_spoken("des fleurs is a 2026 collaboration")['kind'] == "music_content"


def test_jev_uncertainty_never_promotes_to_playback(sdk):
    from plugins.voice_stack import assist_response_labels
    _, answer = sdk
    answer.confidence = float('nan')
    assert assist_response_labels.label_spoken("Playing des fleurs")['kind'] == "unknown"


def test_jev_outage_never_blocks_spoken_answer(monkeypatch):
    from plugins.voice_stack import assist_response_labels
    monkeypatch.setattr(music, "_load_api_key", lambda: "")
    assert assist_response_labels.label_spoken("Playing des fleurs")['kind'] == "unknown"



def test_fast_reply_labels_in_background_without_changing_text(monkeypatch):
    import asyncio
    import threading
    import plugins.voice_stack as voice
    seen = []
    done = threading.Event()
    monkeypatch.setattr(voice, "_route_assist_profile", lambda text: "music")
    monkeypatch.setattr(voice, "_resolve_last_called_media_player", lambda: "")
    monkeypatch.setattr(voice, "_run_full_agent", lambda *a, **kw: "des fleurs is a Stromae collaboration")
    monkeypatch.setattr(voice, "_emit_assist_response_label", lambda text, source: (seen.append((text, source)), done.set()))
    result = asyncio.run(voice._handle_assist_query_with_llm(object(), {"text": "Tell me about des fleurs"}))
    assert result["text"] == "des fleurs is a Stromae collaboration"
    assert done.wait(1)
    assert seen == [(result["text"], "agent_reply")]


def test_label_queue_failure_does_not_replace_fast_reply(monkeypatch):
    import asyncio
    import concurrent.futures
    import plugins.voice_stack as voice
    class Queue:
        def submit(self, fn, *args, **kwargs):
            raise RuntimeError("label queue unavailable")
    monkeypatch.setattr(voice, "ASSIST_LABEL_EXECUTOR", Queue())
    monkeypatch.setattr(voice, "_route_assist_profile", lambda text: "music")
    monkeypatch.setattr(voice, "_resolve_last_called_media_player", lambda: "")
    monkeypatch.setattr(voice, "_run_full_agent", lambda *a, **kw: "Here is the song info")
    result = asyncio.run(voice._handle_assist_query_with_llm(object(), {"text": "Tell me about the song"}))
    assert result["text"] == "Here is the song info"


def test_timeout_ack_is_provenance_labeled_without_jev(monkeypatch):
    import asyncio
    import threading
    import plugins.voice_stack as voice
    release = threading.Event()
    delivered = threading.Event()
    observed = []
    monkeypatch.setattr(voice, "ALEXA_AGENT_BUDGET_SECONDS", 0.01)
    monkeypatch.setattr(voice, "_route_assist_profile", lambda text: "music")
    monkeypatch.setattr(voice, "_resolve_last_called_media_player", lambda: "")
    def slow_agent(*a, **kw):
        release.wait(1)
        return "Finished"
    monkeypatch.setattr(voice, "_run_full_agent", slow_agent)
    monkeypatch.setattr(voice, "_deliver_slow_answer", lambda *a, **kw: delivered.set())
    monkeypatch.setattr(events, "emit", lambda kind, **data: observed.append((kind, data)))
    try:
        result = asyncio.run(voice._handle_assist_query_with_llm(object(), {"text": "Play some music"}))
        assert result["text"] == voice.ALEXA_SLOW_ACK
        assert ("assist_response_label", {"source": "assist_ack", "category": "acknowledgment", "confidence": 1.0}) in observed
    finally:
        release.set()
        assert delivered.wait(1)


def test_ha_service_ack_is_labeled_accepted_not_playing(monkeypatch):
    from plugins.voice_stack import ha_conversation
    monkeypatch.setattr(ha_conversation, "_run_async", lambda coro: (coro.close(), {"ok": True, "target": "echo", "command": "Play song"})[1])
    result = ha_conversation.play_alexa_media("Play song", "echo")
    assert result["acceptance_kind"] == "ha_accepted_unverified"
    assert result["ok"] is True


def test_ha_service_failure_is_not_labeled_accepted(monkeypatch):
    from plugins.voice_stack import ha_conversation
    def fail(coro):
        coro.close()
        raise RuntimeError("HA down")
    monkeypatch.setattr(ha_conversation, "_run_async", fail)
    result = ha_conversation.play_alexa_media("Play song", "echo")
    assert result["acceptance_kind"] == "ha_failed"
    assert result["ok"] is False


def test_ha_nonexceptional_rejection_is_not_accepted(monkeypatch):
    from plugins.voice_stack import ha_conversation
    monkeypatch.setattr(ha_conversation, "_run_async", lambda coro: (coro.close(), {"ok": False, "reason": "rejected"})[1])
    result = ha_conversation.play_alexa_media("Play song", "echo")
    assert result["acceptance_kind"] == "ha_failed"


def test_label_work_uses_separate_executor_from_voice_turns(monkeypatch):
    import asyncio
    import concurrent.futures
    import threading
    import plugins.voice_stack as voice
    started = threading.Event()
    release = threading.Event()
    voice_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    label_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    def blocking_label(*args, **kwargs):
        started.set()
        release.wait(2)
    monkeypatch.setattr(voice, "ALEXA_EXECUTOR", voice_executor)
    monkeypatch.setattr(voice, "ASSIST_LABEL_EXECUTOR", label_executor, raising=False)
    monkeypatch.setattr(voice, "_emit_assist_response_label", blocking_label)
    monkeypatch.setattr(voice, "_route_assist_profile", lambda text: "music")
    monkeypatch.setattr(voice, "_resolve_last_called_media_player", lambda: "")
    monkeypatch.setattr(voice, "_run_full_agent", lambda *a, **kw: "Song info")
    monkeypatch.setattr(voice, "ALEXA_AGENT_BUDGET_SECONDS", 0.1)
    try:
        first = asyncio.run(voice._handle_assist_query_with_llm(object(), {"text": "one"}))
        assert started.wait(1)
        second = asyncio.run(voice._handle_assist_query_with_llm(object(), {"text": "two"}))
        third = asyncio.run(voice._handle_assist_query_with_llm(object(), {"text": "three"}))
        assert first["text"] == second["text"] == third["text"] == "Song info"
        assert not third.get("slow")
    finally:
        release.set()
        label_executor.shutdown(wait=True)
        voice_executor.shutdown(wait=True)


def test_raw_fallback_reply_is_labeled_without_changing_speech(monkeypatch):
    import asyncio
    import threading
    import plugins.voice_stack as voice
    done = threading.Event()
    labels = []
    class LLM:
        async def acomplete(self, **kw):
            return SimpleNamespace(text="Here is a music fact", provider="test", model="test")
    monkeypatch.setattr(voice, "_route_assist_profile", lambda text: "music")
    monkeypatch.setattr(voice, "_resolve_last_called_media_player", lambda: "")
    monkeypatch.setattr(voice, "_run_full_agent", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("api down")))
    monkeypatch.setattr(voice, "_emit_assist_response_label", lambda text, source: (labels.append((text, source)), done.set()))
    result = asyncio.run(voice._handle_assist_query_with_llm(SimpleNamespace(llm=LLM()), {"text": "music?"}))
    assert result["text"] == "Here is a music fact"
    assert done.wait(1)
    assert labels == [(result["text"], "agent_reply")]


def test_slow_reply_is_labeled_but_spoken_unchanged(monkeypatch):
    import plugins.voice_stack as voice
    from plugins.voice_stack import pipeline
    observed = []
    monkeypatch.setattr(pipeline, "play_text_alexa", lambda text, sink: observed.append((text, sink)) or True)
    monkeypatch.setattr(events, "emit", lambda kind, **data: observed.append((kind, data)))
    voice._deliver_slow_answer("Playing des fleurs", target="media_player.echo_dot_5th_left")
    assert observed[0] == ("Playing des fleurs", "media_player.echo_dot_5th_left")
    assert any(x[0] == "assist_response_label" for x in observed)

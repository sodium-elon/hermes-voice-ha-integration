"""Delayed general answers must own a native post-speech capture window."""
import asyncio
import concurrent.futures
import threading
import pytest
from plugins.voice_stack import alexa_confirmation as confirmation
from plugins import voice_stack as voice


@pytest.fixture
def general(monkeypatch):
    now = [0.0]
    pending = confirmation.PendingConfirmation(clock=lambda: now[0])
    launches, notifications = [], []
    monkeypatch.setattr(confirmation, "launch_open_question", lambda target: launches.append(target) or True)
    generation = pending.begin("Explain the tides", None, "general-session")
    def notify(text, *, target): notifications.append((text, target))
    return now, pending, generation, launches, notifications, notify


def test_slow_general_answer_reenters_asking_echo_and_is_spoken_once(general):
    _, pending, g, launches, notifications, notify = general
    pending.deliver(g, "Here is the answer.", "media_player.asking_echo", notify)
    assert launches == ["media_player.asking_echo"]
    assert notifications == []  # do not speak the answer over the old listening window
    assert pending.pending.deadline == 180  # no deadline extension or binary authorization
    pending.deliver(g, "Duplicate.", "media_player.asking_echo", notify)
    assert launches == ["media_player.asking_echo"]
    assert pending.answer("yes") is None
    assert pending.launch() == {"text": "Here is the answer.", "reprompt": confirmation.READY}
    assert pending.pending is None
    assert pending.launch()["text"] == confirmation.READY
    assert notifications == []


def test_general_reentry_failure_falls_back_to_original_answer(general, monkeypatch):
    _, pending, g, _, notifications, notify = general
    monkeypatch.setattr(confirmation, "launch_open_question", lambda _: False)
    pending.deliver(g, "Original answer.", "media_player.asking_echo", notify)
    assert notifications == [("Original answer.", "media_player.asking_echo")]
    assert pending.pending is None


def test_missing_echo_does_not_invent_a_reentry_target(general):
    _, pending, g, launches, notifications, notify = general
    pending.deliver(g, "Original answer.", "", notify)
    assert launches == []
    assert notifications == [("Original answer.", "")]
    assert pending.pending is None


def test_expired_general_result_cannot_reopen_microphone(general):
    now, pending, g, launches, notifications, notify = general
    now[0] = 181
    pending.deliver(g, "Too late.", "media_player.echo", notify)
    assert launches == [] and notifications == []
    assert pending.pending is None


def test_expired_cached_general_reply_is_not_replayed(general):
    now, pending, g, _, _, notify = general
    pending.deliver(g, "Old answer.", "media_player.echo", notify)
    now[0] = 181
    assert pending.launch()["text"] == confirmation.READY
    assert pending.pending is None


def test_new_request_invalidates_older_general_reentry(general):
    _, pending, g, launches, notifications, notify = general
    pending.begin("New task", None, "new-session")
    pending.deliver(g, "Old answer.", "media_player.echo", notify)
    assert launches == [] and notifications == []


@pytest.mark.asyncio
async def test_delayed_general_reply_then_farewell_reaches_closing_judge(monkeypatch):
    pending = confirmation.PendingConfirmation()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    release, launched = threading.Event(), threading.Event()
    calls, targets = [], []
    monkeypatch.setattr(voice, "_ALEXA_PENDING", pending)
    monkeypatch.setattr(voice, "_ALEXA_TURN_LOCK", threading.Lock())
    monkeypatch.setattr(voice, "ALEXA_EXECUTOR", executor)
    monkeypatch.setattr(voice, "ALEXA_AGENT_BUDGET_SECONDS", .001)
    monkeypatch.setattr(voice, "_route_assist_profile", lambda _: None)
    monkeypatch.setattr(voice, "_resolve_last_called_media_player", lambda: "media_player.asking_echo")
    monkeypatch.setattr(voice, "_schedule_assist_label", lambda _: None)
    monkeypatch.setattr(voice.events, "emit", lambda *a, **kw: None)
    monkeypatch.setattr(voice, "_deliver_slow_answer", lambda *a, **kw: pytest.fail("No notification over old capture window"))
    def launch(target):
        targets.append(target)
        launched.set()
        return True
    monkeypatch.setattr(confirmation, "launch_open_question", launch)
    def agent(text, **kw):
        calls.append(text)
        assert release.wait(2)
        return "The answer is now complete."
    monkeypatch.setattr(voice, "_run_full_agent", agent)
    async def ending(text): return text == "Thanks, that's all."
    monkeypatch.setattr(voice.conversation_end, "should_end", ending)
    async def ask(text):
        return await voice._handle_assist_query_with_llm(object(), {
            "text": text, "conversation_id": "alexa_hermes"})
    try:
        result = await ask("Explain the tides")
        assert result["slow"]
        release.set()
        assert launched.wait(2)
        assert targets == ["media_player.asking_echo"]
        reply = await ask(confirmation.LAUNCH)
        assert reply["text"] == "The answer is now complete."
        assert reply["reprompt"] == confirmation.READY
        end = await ask("Thanks, that's all.")
        assert end["text"] == voice.conversation_end.END_SIGNAL
        assert calls == ["Explain the tides"]
    finally:
        release.set()
        executor.shutdown(wait=True)

def test_general_reentry_io_does_not_block_launch_or_duplicate_fallback(general, monkeypatch):
    _, pending, g, _, notifications, notify = general
    entered, release = threading.Event(), threading.Event()
    def network(_):
        entered.set()
        assert release.wait(2)
        return False
    monkeypatch.setattr(confirmation, "launch_open_question", network)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        delivery = pool.submit(pending.deliver, g, "Answer.", "media_player.echo", notify)
        try:
            assert entered.wait(1)
            launch = pool.submit(pending.launch)
            assert launch.result(timeout=.5)["text"] == "Answer."
        finally:
            release.set()
        delivery.result(timeout=1)
    assert notifications == []  # native Launch already consumed it, even if service reply failed


def test_failed_old_reentry_cannot_clear_or_notify_over_new_request(general, monkeypatch):
    _, pending, g, _, notifications, notify = general
    def network(_):
        pending.begin("New question", None, "new-session")
        return False
    monkeypatch.setattr(confirmation, "launch_open_question", network)
    pending.deliver(g, "Old answer.", "media_player.echo", notify)
    assert pending.pending.request == "New question"
    assert notifications == []


def test_general_reentry_exception_falls_back_without_losing_answer(general, monkeypatch):
    _, pending, g, _, notifications, notify = general
    def network(_): raise OSError("unavailable")
    monkeypatch.setattr(confirmation, "launch_open_question", network)
    pending.deliver(g, "Answer.", "media_player.echo", notify)
    assert pending.pending is None
    assert notifications == [("Answer.", "media_player.echo")]

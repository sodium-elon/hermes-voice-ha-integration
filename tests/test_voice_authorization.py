"""Deterministic recorded-turn regressions; no mic, HA, speech, or network."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from plugins.voice_stack import pipeline as mod


@pytest.fixture
def harness(monkeypatch, tmp_path):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("HERMES_VOICE_RETAIN_WAV", raising=False)
    monkeypatch.setattr(mod, "_play_wake_beep", lambda: None)

    calls, spoken, durations, paths = [], [], [], []
    turns = iter(["What's the weather?", "Paris", "Yes"])
    wake_calls = []
    def wake(timeout_seconds):
        wake_calls.append(clock.now)
        if len(wake_calls) == 1:
            return True
        p.state.enabled = False
        return False
    def record(path, **kw):
        paths.append(path)
        durations.append(kw["duration"])
        return True
    monkeypatch.setattr(mod, "record_audio", record)
    def callback(text, **kw):
        calls.append((text, kw))
        return "Which city?"
    p = mod.VoicePipeline(callback, wake_word_engine=SimpleNamespace(listen=wake),
                          stt_engine=SimpleNamespace(transcribe_with_confidence=lambda *a, **k: {
                              "text": next(turns), "confidence": .9}),
                          wake_cooldown=0, follow_up_delay=0)
    monkeypatch.setattr(p, "_speak", lambda text: spoken.append(text) or True)
    p.state.enabled = True
    return SimpleNamespace(p=p, clock=clock, calls=calls, spoken=spoken,
                           durations=durations, paths=paths, wake_calls=wake_calls)


@pytest.fixture
def authorize_timing_turns(monkeypatch):
    """Opt in only when testing timing, not the semantic authorization boundary."""
    monkeypatch.setattr(mod, "authorize_request", lambda *a, **kw: True)


def test_long_reply_cannot_arm_followup_after_wake_deadline(harness, monkeypatch, authorize_timing_turns):
    h = harness
    def callback(text, **kw):
        h.calls.append(text)
        h.clock.now += 61
        return "Which city?"
    h.p._callback = callback
    h.p._run_loop()
    assert h.calls == ["What's the weather?"]
    assert len(h.durations) == 1
    assert not h.p._follow_up_pending
    assert len(h.wake_calls) == 2  # expiry returns to wake detection, not stop()
    assert all(not Path(path).exists() for path in h.paths)


@pytest.mark.parametrize("stage", ["before_delay", "after_delay", "after_capture", "after_stt"])
def test_expired_followup_never_dispatches(harness, monkeypatch, stage, authorize_timing_turns):
    h = harness
    h.p._follow_up_pending = True
    h.p._follow_up_deadline = 160.0
    h.p._follow_up_turns = 1
    # No fresh wake in this test: after expiry, detection remains available.
    def wake(timeout_seconds):
        h.wake_calls.append(h.clock.now)
        h.p.state.enabled = False
        return False
    h.p._wake_word.listen = wake
    if stage == "before_delay":
        h.clock.now = 160.0
    elif stage == "after_delay":
        h.clock.now = 159.0
        h.p._follow_up_delay = 2.0
    elif stage == "after_capture":
        def record(path, **kw):
            h.paths.append(path)
            h.clock.now = 160.0
            return True
        monkeypatch.setattr(mod, "record_audio", record)
    else:
        def stt(*a, **kw):
            h.clock.now = 160.0
            return {"text": "Paris", "confidence": .9}
        h.p._stt.transcribe_with_confidence = stt
    h.p._run_loop()
    assert h.calls == []
    assert h.spoken == []
    assert h.wake_calls
    assert h.p._follow_up_turns == 0
    assert all(not Path(path).exists() for path in h.paths)
    if stage in {"before_delay", "after_delay"}:
        assert h.durations == []


@pytest.mark.parametrize("idle_timeout", ["6", "0", "999"])
def test_followup_capture_is_bounded_by_remaining_wake_lifetime(harness, monkeypatch, idle_timeout, authorize_timing_turns):
    h = harness
    h.p._follow_up_timeout = float(idle_timeout)
    h.p._follow_up_pending = True
    h.p._follow_up_deadline = 160.0
    h.clock.now = 159.5
    def stt(*a, **kw):
        h.p.state.enabled = False
        return {"text": "Paris", "confidence": .9}
    h.p._stt.transcribe_with_confidence = stt
    h.p._run_loop()
    assert h.durations == [pytest.approx(.5)]
    assert h.p._follow_up_deadline == 160.0


INCIDENT_TRANSCRIPTS = [
    "But now, when we were in here on CR, it was actually on mail, which is really...",
    "Yeah, you can see it. I think I got it. It's so crazy. I don't have any control anymore. The AI is working.",
]


@pytest.mark.parametrize("text", INCIDENT_TRANSCRIPTS)
def test_unaddressed_incident_speech_is_silent_before_any_routing(harness, monkeypatch, text):
    h = harness
    judgments = []
    def deny(transcript, **context):
        judgments.append((transcript, context))
        return False
    monkeypatch.setattr(mod, "authorize_request", deny, raising=False)
    h.p._stt.transcribe_with_confidence = lambda *a, **kw: {"text": text, "confidence": .19}
    h.p._run_loop()
    assert judgments == [(text, {"previous_request": None, "previous_question": None})]
    assert h.calls == []  # callback owns music judgments/HA/DJ/general agent work
    assert h.spoken == []
    assert not h.p._follow_up_pending
    assert h.p.state.total_interactions == 0
    assert all(not Path(path).exists() for path in h.paths)


@pytest.fixture
def fake_jev(monkeypatch):
    import sys
    from plugins.voice_stack import music
    state = SimpleNamespace(value=.99, calls=[], error=None, clients=[])
    class Client:
        def __init__(self, **kw):
            state.clients.append(kw)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def system_one(self, **kw):
            state.calls.append(kw)
            if state.error:
                raise state.error
            return SimpleNamespace(answers={"addressed_request": SimpleNamespace(noul=state.value)})
    monkeypatch.setattr(music, "_load_api_key", lambda: "test-key")
    monkeypatch.setitem(sys.modules, "typesafe_sdk", SimpleNamespace(
        TypeSafeClient=Client, Noul=lambda **kw: kw, NoulCriteria=lambda **kw: kw,
        RetryPolicy=lambda **kw: SimpleNamespace(**kw)))
    return state


@pytest.mark.parametrize("text", ["Turn on the kitchen lights", "Play To The Loop", "What's the weather?"])
def test_clear_request_is_authorized_by_one_bounded_semantic_judgment(fake_jev, text):
    from plugins.voice_stack.request_gate import authorize_request
    assert authorize_request(text) is True
    assert len(fake_jev.calls) == 1
    request = fake_jev.calls[0]
    assert request["state"] == {"transcript": text, "previous_request": None, "previous_question": None}
    assert set(request["questions"]) == {"addressed_request"}
    assert fake_jev.clients[0]["timeout"] <= 5
    assert fake_jev.clients[0]["retry"].max_retries == 0


@pytest.mark.parametrize("value", [True, "0.99", float("inf"), float("nan"), 1.01, -.01, None, .5, .89])
def test_invalid_or_uncertain_intent_fails_closed(fake_jev, value):
    from plugins.voice_stack.request_gate import authorize_request
    fake_jev.value = value
    assert authorize_request("Play music") is False


def test_contextual_answer_uses_only_current_authorized_question(harness, monkeypatch):
    h = harness
    judgments = []
    def authorize(text, **context):
        judgments.append((text, context))
        return True
    monkeypatch.setattr(mod, "authorize_request", authorize)
    h.p._run_loop()
    assert judgments == [
        ("What's the weather?", {"previous_request": None, "previous_question": None}),
        ("Paris", {"previous_request": "What's the weather?", "previous_question": "Which city?"}),
        ("Yes", {"previous_request": "Paris", "previous_question": "Which city?"}),
    ]
    assert len(h.calls) == 3
    assert h.p._follow_up_deadline == 160.0
    assert h.p._follow_up_context is None


def test_contextual_jev_judgment_has_explicit_answer_policy(fake_jev):
    from plugins.voice_stack.request_gate import authorize_request
    assert authorize_request("Paris", previous_request="What's the weather?", previous_question="Which city?")
    call = fake_jev.calls[0]
    assert call["state"]["previous_question"] == "Which city?"
    criteria = call["questions"]["addressed_request"]["criteria"]
    assert "contextual answer" in criteria["true"]
    assert "unrelated conversation" in criteria["false"]


def test_fresh_wake_resets_expired_deadline_and_question_context(harness, fake_jev):
    h = harness
    h.p._follow_up_pending = True
    h.p._follow_up_turns = 2
    h.p._follow_up_deadline = 100.0
    h.p._follow_up_context = ("Old request", "Old question?")
    observed = []

    def wake(timeout_seconds):
        h.wake_calls.append(h.clock.now)
        h.clock.now = 200.0
        return True

    def callback(text, **kw):
        observed.append((h.p._follow_up_deadline, h.p._follow_up_turns,
                         h.p._follow_up_context))
        h.p.state.enabled = False
        return "Done."

    h.p._wake_word.listen = wake
    h.p._callback = callback
    h.p._run_loop()

    assert h.wake_calls == [100.0]
    assert observed == [(260.0, 0, None)]
    assert len(h.durations) == 1
    assert fake_jev.calls[0]["state"] == {
        "transcript": "What's the weather?", "previous_request": None,
        "previous_question": None,
    }
    assert not h.p._follow_up_pending
    assert h.p.state.total_errors == 0


def test_replies_and_followup_turns_never_refresh_wake_clock(harness, fake_jev, monkeypatch):
    h = harness
    h.p._max_follow_up_turns = 10  # deadline, not turn count, must end this window
    reply_deadlines = []

    def speak(text):
        h.clock.now += 20.0
        h.spoken.append(text)
        reply_deadlines.append(h.p._follow_up_deadline)
        if len(h.spoken) == 3:
            h.p.state.enabled = False
        return True

    monkeypatch.setattr(h.p, "_speak", speak)
    h.p._run_loop()

    assert [text for text, _ in h.calls] == ["What's the weather?", "Paris", "Yes"]
    assert len(fake_jev.calls) == 3
    assert reply_deadlines == [160.0, 160.0, 160.0]
    assert h.clock.now == 160.0
    assert h.p._follow_up_deadline == 160.0
    assert not h.p._follow_up_pending  # equality expires even a new question
    assert h.p._follow_up_context is None
    assert len(h.wake_calls) == 1
    assert h.p.state.total_errors == 0


def test_valid_low_confidence_short_answer_dispatches_inside_window(harness, fake_jev):
    h = harness
    turns = iter([
        {"text": "What's the weather?", "confidence": .9},
        {"text": "Paris", "confidence": .03},
    ])
    h.p._stt.transcribe_with_confidence = lambda *a, **kw: next(turns)

    def callback(text, **kw):
        h.calls.append((text, kw))
        if text == "Paris":
            h.p.state.enabled = False
            return "It's sunny in Paris."
        h.clock.now = 150.0
        return "Which city?"

    h.p._callback = callback
    h.p._run_loop()

    assert [text for text, _ in h.calls] == ["What's the weather?", "Paris"]
    assert h.calls[1][1]["confidence"] == .03
    assert h.p._follow_up_min_confidence <= .03 < h.p._min_confidence
    assert fake_jev.calls[1]["state"] == {
        "transcript": "Paris", "previous_request": "What's the weather?",
        "previous_question": "Which city?",
    }
    assert len(fake_jev.calls) == 2
    assert h.spoken == ["Which city?", "It's sunny in Paris."]
    assert len(h.wake_calls) == 1
    assert h.p.state.total_interactions == 2
    assert h.p.state.total_errors == 0
    assert h.p._follow_up_deadline == 160.0
    assert not h.p._follow_up_pending
    assert all(not Path(path).exists() for path in h.paths)


def test_semantic_gate_expiry_cannot_dispatch_followup(harness, monkeypatch):
    h = harness
    h.p._follow_up_pending = True
    h.p._follow_up_deadline = 160.0
    def authorize(*a, **kw):
        h.clock.now = 160.0
        return True
    monkeypatch.setattr(mod, "authorize_request", authorize)
    h.p._wake_word.listen = lambda **kw: setattr(h.p.state, "enabled", False) or False
    h.p._run_loop()
    assert h.calls == []
    assert h.spoken == []
    assert all(not Path(path).exists() for path in h.paths)

"""Elapsed authorization bounds with event-controlled evaluators; no external I/O."""
import threading
import time

import pytest


def test_budget_never_exceeds_five_seconds(monkeypatch):
    from plugins.voice_stack import authorization_budget as mod

    now = [100.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])

    def late(text):
        now[0] += 5.01
        return True

    assert mod.authorize_with_budget(late, "Play music", budget_seconds=99) is False


def test_followup_budget_is_capped_by_remaining_wake_deadline():
    from plugins.voice_stack.authorization_budget import authorize_with_budget

    release = threading.Event()
    workers = []
    observed = []

    def stalled(text, **context):
        workers.append(threading.current_thread())
        observed.append(context)
        release.wait()
        return True

    started = time.monotonic()
    try:
        assert authorize_with_budget(
            stalled, "Paris", wake_deadline=started + .03,
            previous_request="Weather?", previous_question="Which city?",
        ) is False
        assert time.monotonic() - started < .5
        assert observed == [{"previous_request": "Weather?", "previous_question": "Which city?"}]
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=1)


def test_stalled_evaluator_denies_within_bounded_wait():
    from plugins.voice_stack.authorization_budget import authorize_with_budget

    release = threading.Event()
    entered = threading.Event()
    workers = []

    def stalled(text, **context):
        workers.append(threading.current_thread())
        entered.set()
        release.wait()
        return True

    started = time.monotonic()
    try:
        assert authorize_with_budget(stalled, "Play music", budget_seconds=.03) is False
        assert entered.is_set()
        assert time.monotonic() - started < .5
        assert workers[0].daemon
        # Timeout must not release the global worker slot.
        extra_calls = []
        assert authorize_with_budget(lambda text: extra_calls.append(text) or True,
                                     "Another request", budget_seconds=.03) is False
        assert extra_calls == []
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=1)
    # The old worker's late True is never reused, even for an identical request.
    assert authorize_with_budget(lambda text: False, "Play music") is False
    assert authorize_with_budget(lambda text: True, "Fresh request") is True


def test_concurrent_gate_denies_without_starting_another_worker():
    from plugins.voice_stack.authorization_budget import authorize_with_budget

    entered, release = threading.Event(), threading.Event()
    calls, results = [], []

    def evaluate(text):
        calls.append(text)
        entered.set()
        release.wait()
        return True

    caller = threading.Thread(target=lambda: results.append(
        authorize_with_budget(evaluate, "First", budget_seconds=1)))
    caller.start()
    try:
        assert entered.wait(timeout=1)
        assert authorize_with_budget(evaluate, "Second") is False
        assert calls == ["First"]
    finally:
        release.set()
        caller.join(timeout=1)
    assert not caller.is_alive()
    assert results == [True]


@pytest.mark.parametrize("result", [False, 1, "true", None])
def test_only_explicit_true_allows(result):
    from plugins.voice_stack.authorization_budget import authorize_with_budget
    assert authorize_with_budget(lambda text: result, "Request") is False


def test_worker_errors_fail_closed_and_free_slot_after_exit():
    from plugins.voice_stack.authorization_budget import authorize_with_budget

    def broken(text):
        raise RuntimeError("SDK failed")

    assert authorize_with_budget(broken, "Request") is False
    assert authorize_with_budget(lambda text: True, "New request") is True


@pytest.mark.parametrize("budget", [0, -1])
def test_exhausted_budget_does_not_start_evaluator(budget):
    from plugins.voice_stack.authorization_budget import authorize_with_budget
    calls = []
    assert authorize_with_budget(lambda text: calls.append(text) or True,
                                 "Request", budget_seconds=budget) is False
    assert calls == []


def test_pipeline_preserves_mock_seam_and_supplies_followup_deadline(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from plugins.voice_stack import pipeline as mod

    calls, judgments = [], []
    evaluator = lambda *a, **kw: True
    monkeypatch.setattr(mod, "authorize_request", evaluator)
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(mod.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(mod, "_play_wake_beep", lambda: None)
    monkeypatch.setattr(mod.events, "emit", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "record_audio", lambda *a, **kw: True)
    monkeypatch.delenv("HERMES_VOICE_RETAIN_WAV", raising=False)

    def bounded(evaluate, text, **kwargs):
        judgments.append((evaluate, text, kwargs))
        return False

    monkeypatch.setattr(mod, "authorize_with_budget", bounded, raising=False)

    def transcribe(*a, **kw):
        p.state.enabled = False
        return {"text": "Paris", "confidence": .9}

    p = mod.VoicePipeline(lambda text, **kw: calls.append(text) or "",
                          stt_engine=SimpleNamespace(transcribe_with_confidence=transcribe),
                          follow_up_delay=0)
    p.state.enabled = True
    p._follow_up_pending = True
    p._follow_up_deadline = 100.1
    p._follow_up_context = ("Weather?", "Which city?")
    p._run_loop()
    assert calls == []
    assert judgments == [(evaluator, "Paris", {
        "wake_deadline": 100.1, "previous_request": "Weather?",
        "previous_question": "Which city?",
    })]

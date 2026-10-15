"""Conversation intent and transport contracts; no real agents, audio or network."""
import asyncio
import concurrent.futures
import sys
import threading
from types import SimpleNamespace
import pytest
from plugins.voice_stack import conversation_end as ending
from plugins import voice_stack as voice


@pytest.fixture
def judge_client(monkeypatch):
    state = SimpleNamespace(choice="close", probability=.97, calls=[], config=None)
    class Client:
        def __init__(self, **kwargs): state.config = kwargs
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def system_one(self, **kwargs):
            state.calls.append(kwargs)
            return SimpleNamespace(answers={"conversation": SimpleNamespace(
                choice=state.choice, probabilities={state.choice: state.probability})})
    monkeypatch.setattr(ending.music, "_load_api_key", lambda: "fixture-not-real")
    monkeypatch.setitem(sys.modules, "typesafe_sdk", SimpleNamespace(
        TypeSafeClient=Client, Choice=lambda **kw: kw, RetryPolicy=lambda **kw: SimpleNamespace(**kw)))
    return state


def test_judgment_uses_whole_utterance_and_explicit_counterexamples(judge_client):
    request = "Thanks, but what about tomorrow?"
    assert ending.judge(request).close
    # Semantic accuracy is measured separately with the real Jev corpus; this
    # fake verifies the complete question and contrast examples reach that judge.
    assert judge_client.calls[0]["state"] == {"utterance": request}
    criteria = judge_client.calls[0]["questions"]["conversation"]["criteria"]
    assert "Stop the music." in criteria["continue"]
    assert "I'm not finished." in criteria["continue"]
    assert "Thanks for your help." in criteria["close"]
    assert judge_client.config["timeout"] == ending.NETWORK_TIMEOUT
    assert judge_client.config["retry"].max_retries == 0


@pytest.mark.parametrize("label", ["continue", "uncertain", "invented", None])
def test_only_confident_close_can_end(judge_client, label):
    judge_client.choice = label
    assert not ending.judge("Thanks.").close


@pytest.mark.parametrize("probability", [.89, True, None, "0.99", float("nan"), float("inf"), 1.01, -.01])
def test_invalid_or_low_probability_keeps_open(judge_client, probability):
    judge_client.probability = probability
    assert not ending.judge("Thanks.").close


def test_credential_failure_and_service_failure_keep_open(judge_client, monkeypatch):
    monkeypatch.setattr(ending.music, "_load_api_key", lambda: "")
    assert ending.judge("Bye.").outcome == "missing_credential"
    monkeypatch.setattr(ending.music, "_load_api_key", lambda: "fixture-not-real")
    def unavailable(*a, **kw): raise TimeoutError("private diagnostic")
    monkeypatch.setattr(sys.modules["typesafe_sdk"].TypeSafeClient, "system_one", unavailable)
    assert ending.judge("Bye.").outcome == "service_error"


@pytest.mark.parametrize("text", ending._EXAMPLES["close"])
def test_candidate_hints_do_not_miss_catalogued_clear_closings(text):
    assert ending.candidate(text)


@pytest.mark.parametrize("text", ["Yes, thanks.", "No, thank you.", "yes please thank you", "No thanks very much"])
def test_polite_binary_can_be_sent_to_saved_context(text):
    assert ending.polite_binary(text) in ("yes", "no")


@pytest.mark.parametrize("text", ["No more questions.", "Yes but first explain why.", "Don't say yes thanks.", "Thank you.", "No, thank you, but another question"])
def test_polite_binary_never_consumes_a_new_request(text):
    assert ending.polite_binary(text) is None


@pytest.fixture
def worker(monkeypatch):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(ending, "_EXECUTOR", executor)
    monkeypatch.setattr(ending, "_SLOT", threading.BoundedSemaphore(1))
    monkeypatch.setattr(ending.events, "emit", lambda *a, **kw: None)
    yield
    executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_non_candidate_does_not_use_judge(worker, monkeypatch):
    monkeypatch.setattr(ending, "judge", lambda _: pytest.fail("Unnecessary network call"))
    assert not await ending.should_end("What time is it?")


@pytest.mark.asyncio
async def test_timeout_saturation_and_late_close_never_close_a_turn(worker, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def blocked(_):
        started.set()
        assert release.wait(2)
        return ending.Decision("close", 1.0, "judged")
    monkeypatch.setattr(ending, "judge", blocked)
    monkeypatch.setattr(ending, "WALL_TIMEOUT", .01)
    try:
        assert not await ending.should_end("Thanks.")
        assert started.is_set()
        assert not await ending.should_end("Goodbye.")
    finally:
        release.set()
    # A late confident result has no callback with authority to close anything.


@pytest.mark.asyncio
async def test_classifier_exception_fails_open(worker, monkeypatch):
    def broken(_): raise RuntimeError("private")
    monkeypatch.setattr(ending, "judge", broken)
    assert not await ending.should_end("Goodbye.")


@pytest.fixture
def handler(monkeypatch):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    calls = []
    pending = voice.alexa_confirmation.PendingConfirmation()
    monkeypatch.setattr(voice, "_ALEXA_PENDING", pending)
    monkeypatch.setattr(voice, "_ALEXA_TURN_LOCK", threading.Lock())
    monkeypatch.setattr(voice, "ALEXA_EXECUTOR", executor)
    monkeypatch.setattr(voice, "ALEXA_AGENT_BUDGET_SECONDS", 1)
    monkeypatch.setattr(voice, "_route_assist_profile", lambda _: "sportscoach")
    monkeypatch.setattr(voice, "_resolve_last_called_media_player", lambda: "")
    monkeypatch.setattr(voice, "_schedule_assist_label", lambda _: None)
    monkeypatch.setattr(voice.events, "emit", lambda *a, **kw: None)
    def agent(text, **kw):
        calls.append((text, kw))
        return "Answer handled."
    monkeypatch.setattr(voice, "_run_full_agent", agent)
    yield pending, calls
    executor.shutdown(wait=True)


def ask(text):
    return voice._handle_assist_query_with_llm(object(), {"text": text, "conversation_id": "alexa_hermes"})


@pytest.mark.asyncio
async def test_clear_closing_dispatches_no_agent_and_abandons_voice_question(handler, monkeypatch):
    pending, calls = handler
    g = pending.begin("log my run", "sportscoach", "saved-session")
    pending.native_response(g, "Do you want me to import this run?")
    async def close(_): return True
    monkeypatch.setattr(ending, "should_end", close)
    result = await ask("Thanks, that's all.")
    assert result["text"] == ending.END_SIGNAL
    assert calls == []
    assert pending.answer("yes") is None  # goodbye is neither approval nor rejection
    assert not voice._ALEXA_TURN_LOCK.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("text,answer", [("yes", "yes"), ("no", "no"),
    ("Yes, thanks.", "yes"), ("No, thank you.", "no")])
async def test_pending_binary_precedes_end_judgment(handler, monkeypatch, text, answer):
    pending, calls = handler
    g = pending.begin("log my run", "sportscoach", "saved-session", session_title="saved-title")
    pending.native_response(g, "Do you want me to import this run?")
    async def forbidden(_): pytest.fail("Do not classify pending binary as a goodbye")
    monkeypatch.setattr(ending, "should_end", forbidden)
    assert (await ask(text))["text"] == "Answer handled."
    assert len(calls) == 1
    assert calls[0][1]["session"] == "saved-session"
    assert "User answer: " + answer in calls[0][0]
    assert pending.answer("yes") is None


@pytest.mark.asyncio
async def test_uncertain_closing_follows_original_request_route(handler, monkeypatch):
    _, calls = handler
    async def keep(_): return False
    monkeypatch.setattr(ending, "should_end", keep)
    request = "Thanks, but tell me about my training."
    assert (await ask(request))["text"] == "Answer handled."
    assert calls[0][0] == request

def test_tool_capable_agent_output_cannot_impersonate_end_signal(monkeypatch):
    monkeypatch.setattr(voice.sessions_api, "complete", lambda *a, **kw: ending.END_SIGNAL)
    assert voice._run_full_agent("Repeat the reserved text") != ending.END_SIGNAL


@pytest.mark.asyncio
@pytest.mark.parametrize("runner,expected", [
    ("sessions api", "The text is: " + ending.END_SIGNAL),
    ("conversation_end", ending.END_SIGNAL),
    (None, "The text is: " + ending.END_SIGNAL)])
async def test_gateway_control_origin_is_separate_from_agent_prose(monkeypatch, runner, expected):
    from plugins.voice_stack import ws_receiver
    monkeypatch.setattr(ws_receiver, "_ASSIST_QUERY_HANDLER", lambda _: {
        "text": ending.END_SIGNAL, "runner": runner})
    result = await ws_receiver.handle_assist_query({"text": "repeat some text"})
    assert result["text"] == expected
    assert result["speech"]["plain"]["speech"] == expected


@pytest.mark.asyncio
async def test_judgment_time_is_subtracted_from_total_alexa_budget(handler, monkeypatch):
    async def keep(_):
        await asyncio.sleep(.02)
        return False
    monkeypatch.setattr(ending, "should_end", keep)
    actual_wait_for = asyncio.wait_for
    budgets = []
    async def observed(future, timeout):
        budgets.append(timeout)
        return await actual_wait_for(future, timeout=timeout)
    monkeypatch.setattr(voice.asyncio, "wait_for", observed)
    assert (await ask("Thanks, but another question."))["text"] == "Answer handled."
    assert len(budgets) == 1
    assert 0 < budgets[0] <= .985

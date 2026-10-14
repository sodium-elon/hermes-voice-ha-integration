"""Choice authorization contracts; no network, recording or dispatch."""
import sys
from types import SimpleNamespace
import pytest
from plugins.voice_stack import request_gate

@pytest.fixture
def judge(monkeypatch):
    state = SimpleNamespace(label="general_information", probability=.96, calls=[])
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def system_one(self, **kwargs):
            state.calls.append(kwargs)
            return SimpleNamespace(model="test", answers={"addressed_request": SimpleNamespace(
                choice=state.label, probabilities={state.label: state.probability}, confidence=.95)})
    monkeypatch.setattr(request_gate.music, "_load_api_key", lambda: "test")
    monkeypatch.setitem(sys.modules, "typesafe_sdk", SimpleNamespace(
        TypeSafeClient=Client, Choice=lambda **kwargs: kwargs,
        RetryPolicy=lambda **kwargs: SimpleNamespace(**kwargs)))
    return state

def test_choice_and_failure_reasons_are_observable(judge, monkeypatch):
    emitted = []
    monkeypatch.setattr(request_gate.events, "emit", lambda kind, **data: emitted.append((kind, data)))
    decision = {}
    assert request_gate.authorize_request("What's the biggest city in Asia?", decision_out=decision)
    assert decision == {"choice": "general_information"}
    assert emitted[-1][1]["probability"] == .96
    monkeypatch.setattr(request_gate.music, "_load_api_key", lambda: "")
    assert not request_gate.authorize_request("What's the biggest city in Asia?")
    assert emitted[-1][1]["outcome"] == "missing_credential"


def test_service_failure_is_denied_without_dispatch(judge, monkeypatch):
    def broken(*args, **kwargs):
        raise TimeoutError("private service details")
    monkeypatch.setattr(sys.modules["typesafe_sdk"].TypeSafeClient, "system_one", broken)
    decision = {}
    assert not request_gate.authorize_request("What's the biggest city in Asia?", decision_out=decision)
    assert decision == {}


def test_general_information_question_without_repeated_name_is_allowed(judge):
    assert request_gate.authorize_request("What's the biggest city in Asia?")
    question = judge.calls[0]["questions"]["addressed_request"]
    assert "general_information" in question["criteria"]
    assert "Ria Sunn" in question["criteria"]["general_information"]

@pytest.mark.parametrize("label", ["music", "sportscoach", "device_request", "other_request"])
def test_existing_request_domains_remain_authorized(judge, label):
    judge.label = label
    assert request_gate.authorize_request("A clear request")

@pytest.mark.parametrize("label", ["background_speech", "unclear", "invented", "contextual_answer"])
def test_nonrequests_or_unprompted_answers_are_denied(judge, label):
    judge.label = label
    assert not request_gate.authorize_request("Some speech")

def test_contextual_answer_requires_both_context_fields(judge):
    judge.label = "contextual_answer"
    assert request_gate.authorize_request("Paris", previous_request="Weather?", previous_question="Which city?")
    assert not request_gate.authorize_request("Paris", previous_question="Which city?")

@pytest.mark.parametrize("value", [.89, True, None, "0.99", float("nan"), float("inf"), 1.01, -.01])
def test_invalid_or_uncertain_probability_is_denied(judge, value):
    judge.probability = value
    assert not request_gate.authorize_request("What's the biggest city in Asia?")

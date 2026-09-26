"""Hermetic routing contracts: no real inference, DB, audio, or event log."""
import importlib
import importlib.util
import json
import sys
from types import SimpleNamespace

import pytest

from plugins.voice_stack import events, music


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected external or playback call")
    monkeypatch.setattr(music, "_load_api_key", lambda: "")
    monkeypatch.setattr(music, "_duckdb_artist_candidates", forbidden)
    monkeypatch.setattr(music, "_postgres_artist_candidates", forbidden)
    monkeypatch.setattr(music, "resolve", forbidden)
    monkeypatch.setattr(events, "emit", lambda *args, **kwargs: None)


@pytest.fixture
def handoff():
    return importlib.import_module("plugins.voice_stack.music_handoff")


@pytest.fixture
def fake_sdk(monkeypatch):
    calls = []
    response = SimpleNamespace(
        answers={"music": SimpleNamespace(noul=0.83)}, model="jev-test"
    )

    class Client:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def system_one(self, **kwargs):
            calls.append(("request", kwargs))
            if isinstance(response.answers, Exception):
                raise response.answers
            return response

    monkeypatch.setitem(sys.modules, "typesafe_sdk", SimpleNamespace(
        TypeSafeClient=Client, Noul=SimpleNamespace, NoulCriteria=SimpleNamespace
    ))
    monkeypatch.setattr(music, "_load_api_key", lambda: "test-credential")
    return calls, response


def test_module_available():
    assert importlib.util.find_spec("plugins.voice_stack.music_handoff") is not None


def test_judge_uses_noul_explicit_criteria_and_private_telemetry(handoff, fake_sdk, monkeypatch):
    calls, _ = fake_sdk
    emitted = []
    monkeypatch.setattr(events, "emit", lambda kind, **fields: emitted.append((kind, fields)))
    state = {"hypotheses": [{"text": "private transcript", "source": "stt"}], "artist_hints": []}
    assert handoff.judge_music(state) == 0.83
    assert calls[0][1]["api_key"] == "test-credential"
    request = calls[1][1]
    assert request["state"] == state
    assert request["model"] == "jev-latest"
    question = request["questions"]["music"]
    assert "hypotheses" in question.instructions
    assert "untrusted" in question.instructions.lower()
    assert "instructions" in question.instructions.lower()
    assert "music" in question.criteria.true.lower()
    for negative in ("household", "tv", "wake", "artifact"):
        assert negative in question.criteria.false.lower()
    assert emitted == [("music_handoff_judgment", {"model": "jev-test", "probability": 0.83})]
    assert "private transcript" not in json.dumps(emitted)
    assert "test-credential" not in json.dumps(emitted)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.1, None, True, "0.9", "invalid"])
def test_invalid_probability_is_unavailable(handoff, fake_sdk, bad):
    fake_sdk[1].answers["music"].noul = bad
    with pytest.raises(music.TypeSafeUnavailable):
        handoff.judge_music({"hypotheses": [], "artist_hints": []})


def test_missing_key_is_unavailable(handoff):
    with pytest.raises(music.TypeSafeUnavailable):
        handoff.judge_music({})


@pytest.mark.parametrize("failure", [RuntimeError("secret error"), {}, {"music": object()}])
def test_api_and_response_errors_are_unavailable(handoff, fake_sdk, failure):
    fake_sdk[1].answers = failure
    with pytest.raises(music.TypeSafeUnavailable) as error:
        handoff.judge_music({})
    assert "secret error" not in str(error.value)


def test_artist_hints_use_suffixes_and_keep_candidate_identity_unasserted(handoff, monkeypatch):
    calls = []
    def duck(span, limit):
        calls.append((span, limit))
        return ["The Beatles", "The Beatles", "the beatles", "Bee Gees"]
    monkeypatch.setattr(music, "_duckdb_artist_candidates", duck)
    hypotheses = [{"text": "Face the beaters", "source": "whisper"}]
    hints = handoff.collect_artist_hints(hypotheses)
    assert ("the beaters", 5) in calls
    assert len(calls) <= 6
    assert all(set(h) == {"span", "source", "candidates"} for h in hints)
    assert all(h["source"] == "duckdb" for h in hints)
    assert all(h["candidates"] == ["The Beatles", "Bee Gees"] for h in hints)
    assert hypotheses == [{"text": "Face the beaters", "source": "whisper"}]


@pytest.mark.parametrize("text", ["Play the beaters", "song by the beaters", "Play song by the beaters"])
def test_strip_leading_request_words(handoff, monkeypatch, text):
    spans = []
    monkeypatch.setattr(music, "_duckdb_artist_candidates", lambda span, limit: spans.append(span) or ["The Beatles"])
    handoff.collect_artist_hints(text)
    assert spans[0] == "the beaters"


def test_hint_query_budget_dedup_and_string_candidates(handoff, monkeypatch):
    calls = []
    def duck(span, limit):
        calls.append((span, limit))
        return [None, 5, "", " A ", "a", "B", "C", "D", "E", "F"]
    monkeypatch.setattr(music, "_duckdb_artist_candidates", duck)
    hypotheses = ["one two three four five six", "one two three four five six"] * 20
    hints = handoff.collect_artist_hints(hypotheses)
    assert 0 < len(calls) <= 6
    assert len({span.casefold() for span, _ in calls}) == len(calls)
    assert all(len(span.split()) <= 4 and limit == 5 for span, limit in calls)
    assert all(h["candidates"] == ["A", "B", "C", "D", "E"] for h in hints)


@pytest.mark.parametrize("duck_result", [[], None, RuntimeError("DB offline")])
def test_postgres_fallback_has_truthful_provenance(handoff, monkeypatch, duck_result):
    def duck(*args, **kwargs):
        if isinstance(duck_result, Exception):
            raise duck_result
        return duck_result
    calls = []
    monkeypatch.setattr(music, "_duckdb_artist_candidates", duck)
    monkeypatch.setattr(music, "_postgres_artist_candidates", lambda span, limit: calls.append((span, limit)) or ["Björk"])
    hints = handoff.collect_artist_hints({"transcript": "Play Björk", "source": "stt"})
    assert hints == [{"span": "Björk", "source": "postgres", "candidates": ["Björk"]}]
    assert calls == [("Björk", 5)]


def test_db_outage_returns_no_hints_with_bounded_work(handoff, monkeypatch):
    calls = []
    def offline(*args, **kwargs):
        calls.append(args)
        raise OSError("Unavailable")
    monkeypatch.setattr(music, "_duckdb_artist_candidates", offline)
    monkeypatch.setattr(music, "_postgres_artist_candidates", offline)
    assert handoff.collect_artist_hints(["some long possibly musical phrase"] * 20) == []
    assert len(calls) <= 12


@pytest.mark.parametrize("hypotheses", [[], None, "", "!?123", {"source": "not a transcript"}])
def test_empty_or_nontext_evidence_does_not_query(handoff, hypotheses):
    assert handoff.collect_artist_hints(hypotheses) == []

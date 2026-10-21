"""Voice exclusions must finish before any DJ session/selection begins."""

import pytest
from plugins import voice_stack as voice
from plugins.voice_stack import music_request_limits as limits


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(limits.events, "emit", lambda *a, **k: None)
    monkeypatch.setattr(limits, "confirmed_alias", lambda *a: None)
    monkeypatch.setattr(limits, "evidence", lambda: (["Stromae", "U2"], ["Stromae"]))


@pytest.mark.parametrize(
    "utterance",
    [
        "play something inspiring to go doing a workout but not strong like",
        "I told you specifically not to play strong life",
        "play some workout music but not Stromae",
    ],
)
def test_resolved_limit_reaches_dj_before_selection(monkeypatch, utterance):
    order = []

    def classify(*args):
        order.append("interpret")
        return "Stromae"

    def dj(text, **kwargs):
        order.append("dj")
        assert utterance in text
        assert "Excluded artist for this request: Stromae." in text
        return "response"

    monkeypatch.setattr(limits, "classify", classify)
    monkeypatch.setattr(voice.sessions_api, "complete", dj)
    assert (
        voice._run_full_agent(utterance, profile="music")
        == "Passed your request to DJ Yakkuza, excluding Stromae. response"
    )
    assert order == ["interpret", "dj"]


def test_unclear_never_starts_dj(monkeypatch):
    monkeypatch.setattr(limits, "classify", lambda *args: None)
    monkeypatch.setattr(
        voice.sessions_api,
        "complete",
        lambda *a, **k: pytest.fail("DJ started before clarification"),
    )
    assert (
        voice._run_full_agent("play something but not unclear name", profile="music")
        == limits.CLARIFY
    )


def test_classifier_outage_never_starts_dj(monkeypatch):
    def unavailable(*args):
        raise RuntimeError("offline")

    monkeypatch.setattr(limits, "classify", unavailable)
    monkeypatch.setattr(
        voice.sessions_api,
        "complete",
        lambda *a, **k: pytest.fail("DJ started on ambiguous utterance"),
    )
    assert (
        voice._run_full_agent("play something but not unclear name", profile="music")
        == limits.CLARIFY
    )


@pytest.mark.parametrize(
    "utterance", ["play inspiring music for training", "good choice I liked this"]
)
def test_no_exclusion_does_not_change_selection_or_feedback(monkeypatch, utterance):
    monkeypatch.setattr(
        limits, "classify", lambda *a: pytest.fail("unneeded inference")
    )
    assert limits.prepare(utterance) == {"text": utterance}


def test_literal_limitation_preserved(monkeypatch):
    monkeypatch.setattr(limits, "classify", lambda *a: "")
    assert limits.prepare("play something without lyrics") == {
        "text": "play something without lyrics"
    }


def test_confirmed_transcription_alias_is_only_a_limitation(monkeypatch, tmp_path):
    import json
    from pathlib import Path
    from importlib import reload

    reload(limits)
    monkeypatch.setattr(limits.events, "emit", lambda *a, **k: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = (
        tmp_path
        / ".hermes/profiles/music/skills/music-domain/references/artist-transcription-aliases.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"strong like": "Stromae", "strong life": "Stromae"}))
    monkeypatch.setattr(limits, "evidence", lambda: (["Stromae"], ["Stromae"]))
    monkeypatch.setattr(
        limits,
        "classify",
        lambda *a: pytest.fail("confirmed correction needs no inference"),
    )
    for phrase in (
        "play workout music but not strong like",
        "I told you not to play strong life",
    ):
        result = limits.prepare(phrase)
        assert result["excluded_artist"] == "Stromae"
        assert phrase in result["text"]
        assert "not a permanent dislike" in result["text"]
    assert limits.confirmed_alias("play Strong Life", ["Stromae"]) is None
    assert limits.confirmed_alias("not the song Strong Life", ["Stromae"]) is None
    assert limits.confirmed_alias("not strong like", ["U2"]) is None

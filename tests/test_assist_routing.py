"""Hermetic tests for the HA-assist specialist router (sportscoach | music | general).

No real inference, DB, audio, or event log — the Jev Choice is faked.
"""
import importlib
import sys
from types import SimpleNamespace

import pytest

from plugins.voice_stack import events, music


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected external call")
    monkeypatch.setattr(music, "_load_api_key", lambda: "")
    monkeypatch.setattr(events, "emit", lambda *args, **kwargs: None)


@pytest.fixture
def voice():
    return importlib.import_module("plugins.voice_stack")


@pytest.fixture
def fake_route_sdk(monkeypatch):
    """Fake typesafe_sdk whose single Choice answer is configurable."""
    state = {"choice": "neither", "confidence": 0.9}

    class Answer:
        @property
        def choice(self):
            return state["choice"]

        @property
        def confidence(self):
            return state["confidence"]

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def system_one(self, **kwargs):
            return SimpleNamespace(answers={"route": Answer()}, model="jev-test")

    monkeypatch.setitem(sys.modules, "typesafe_sdk", SimpleNamespace(
        TypeSafeClient=Client, Choice=SimpleNamespace, Noul=SimpleNamespace,
        NoulCriteria=SimpleNamespace,
    ))
    monkeypatch.setattr(music, "_load_api_key", lambda: "test-credential")
    return state


def _route(voice, text):
    return voice._route_assist_profile(text)


def test_running_regex_fast_path_routes_to_sportscoach(voice, monkeypatch):
    # Fast path must not even need the fake SDK — pure regex.
    monkeypatch.setattr(voice, "_jev_route_specialist", lambda t: pytest.fail("regex should win"))
    assert _route(voice, "What is my fastest run in august") == "sportscoach"


def test_music_choice_routes_to_music(voice, fake_route_sdk):
    fake_route_sdk["choice"] = "music"
    fake_route_sdk["confidence"] = 0.85
    assert _route(voice, "Play the Beatles") == "music"


def test_sports_choice_routes_to_sportscoach(voice, fake_route_sdk):
    fake_route_sdk["choice"] = "sportscoach"
    fake_route_sdk["confidence"] = 0.8
    assert _route(voice, "How far did I run last week?") == "sportscoach"


def test_neither_choice_routes_to_general(voice, fake_route_sdk):
    fake_route_sdk["choice"] = "neither"
    fake_route_sdk["confidence"] = 0.95
    assert _route(voice, "What is the weather today?") is None


def test_low_confidence_specialist_falls_to_general(voice, fake_route_sdk):
    fake_route_sdk["choice"] = "music"
    fake_route_sdk["confidence"] = 0.4
    assert _route(voice, "y'know, something like a tune?") is None


def test_missing_key_routes_to_general(voice, monkeypatch):
    monkeypatch.setattr(music, "_load_api_key", lambda: "")
    assert _route(voice, "Play some music") is None


def test_sdk_exception_fails_closed_to_general(voice, monkeypatch):
    class Broken:
        def __init__(self, **kwargs):
            raise RuntimeError("down")

    monkeypatch.setitem(sys.modules, "typesafe_sdk", SimpleNamespace(
        TypeSafeClient=Broken, Choice=SimpleNamespace, Noul=SimpleNamespace,
        NoulCriteria=SimpleNamespace,
    ))
    monkeypatch.setattr(music, "_load_api_key", lambda: "test-credential")
    assert _route(voice, "Play some music") is None
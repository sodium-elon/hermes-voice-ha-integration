"""Regression tests for the existing-route Alexa confirmation experiment.

These tests are intentionally hermetic: no Home Assistant, no Alexa, no agent,
no network. They verify the contract of the bounded pending confirmation record
and the static wiring points that make the HA/Alexa route safe to deploy.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from plugins.voice_stack import alexa_confirmation

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _source(rel_path: str) -> str:
    return (ROOT / rel_path).read_text()


def test_constants_match_ha_alexa_contract() -> None:
    assert alexa_confirmation.CONVERSATION_ID == "alexa_hermes"
    assert alexa_confirmation.LAUNCH == "__hermes_alexa_launch__"
    assert alexa_confirmation.YES == "__hermes_alexa_yes__"
    assert alexa_confirmation.NO == "__hermes_alexa_no__"
    assert alexa_confirmation.SKILL_ID.startswith("amzn1.ask.skill.")


@pytest.mark.parametrize(
    "reply",
    [
        "I found two matches. Want me to play the first one?",
        "That track is available. Should I play it?",
        "I can use Apple Music. Would you like me to start it?",
        "This looks right. Do you want me to play it?",
        "I found the song. Shall I send it to Alexa?",
        "I found it. Can I play it?",
        "Good pick. Want to hear it?",
    ],
)
def test_eligible_confirmation_accepts_dj_yes_no_questions(reply: str) -> None:
    assert alexa_confirmation.eligible_confirmation("music", reply)


@pytest.mark.parametrize(
    "profile,reply",
    [
        ("default", "Want me to play it?"),
        ("music", "I sent it to Alexa."),
        ("music", "Which version is best?"),
        ("music", "Play the song? Maybe."),
        ("music", ""),
        ("music", "x" * 601 + " Want me to play it?"),
    ],
)
def test_eligible_confirmation_rejects_non_music_or_non_yes_no(profile: str, reply: str) -> None:
    assert not alexa_confirmation.eligible_confirmation(profile, reply)


COACH_REPLY = (
    "Found your October third run: 5.64 kilometers in 31 minutes 19 seconds, "
    "at 5:33 per kilometer; kjordan matches your Qiaodan Leili 2.0 shoes. "
    "It isn’t logged yet—since it’s near midnight locally, "
    "confirm October third is the date you want imported."
)


def test_coach_delayed_confirmation_reopens_existing_skill(monkeypatch) -> None:
    pending = alexa_confirmation.PendingConfirmation(clock=Clock())
    generation = pending.begin("get today's run using kjordan", "sportscoach", "alexa_hermes")
    launches, notifications = [], []
    monkeypatch.setattr(alexa_confirmation, "launch_skill", lambda target: launches.append(target) or True)

    pending.deliver(generation, COACH_REPLY, "media_player.echo", lambda text, target=None: notifications.append(text))

    assert launches == ["media_player.echo"]
    assert notifications == []
    assert pending.launch() == {"text": COACH_REPLY, "reprompt": alexa_confirmation.ANSWER_REPROMPT}
    work = pending.answer("yes")
    assert work is not None
    assert work["profile"] == "sportscoach"
    assert work["session"] == "alexa_hermes"
    assert "Original request: get today's run using kjordan" in work["text"]
    assert COACH_REPLY in work["text"]
    assert "User answer: yes" in work["text"]
    assert pending.answer("yes") is None


def test_launch_without_pending_returns_ready_prompt_and_reprompt() -> None:
    pending = alexa_confirmation.PendingConfirmation(clock=Clock())

    assert pending.launch() == {
        "text": alexa_confirmation.READY,
        "reprompt": alexa_confirmation.READY,
    }


def test_begin_returns_monotonic_generation_and_replaces_prior_pending() -> None:
    clock = Clock()
    pending = alexa_confirmation.PendingConfirmation(clock=clock)

    first = pending.begin("old", "music", "alexa_hermes")
    second = pending.begin("new", "music", "alexa_hermes")

    assert first == 1
    assert second == 2
    work = pending.answer("yes")
    assert work is None  # no reply has been armed yet


def test_deliver_non_confirmation_notifies_once_and_clears_pending() -> None:
    clock = Clock()
    pending = alexa_confirmation.PendingConfirmation(clock=clock)
    generation = pending.begin("play swedish music", "music", "alexa_hermes")
    calls = []

    pending.deliver(generation, "I sent it to Alexa.", "media_player.echo", lambda text, target=None: calls.append((text, target)))

    assert calls == [("I sent it to Alexa.", "media_player.echo")]
    assert pending.answer("yes") is None


def test_deliver_confirmation_relaunches_skill_and_does_not_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    pending = alexa_confirmation.PendingConfirmation(clock=clock)
    generation = pending.begin("find a song", "music", "alexa_hermes")
    launches = []
    notifications = []

    monkeypatch.setattr(alexa_confirmation, "launch_skill", lambda target: launches.append(target) or True)

    pending.deliver(
        generation,
        "I found a good match. Want me to play it?",
        "media_player.echo",
        lambda text, target=None: notifications.append((text, target)),
    )

    assert launches == ["media_player.echo"]
    assert notifications == []
    assert pending.launch() == {
        "text": "I found a good match. Want me to play it?",
        "reprompt": alexa_confirmation.ANSWER_REPROMPT,
    }


def test_deliver_confirmation_launch_failure_speaks_recovery_and_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    pending = alexa_confirmation.PendingConfirmation(clock=clock)
    generation = pending.begin("find a song", "music", "alexa_hermes")
    notifications = []

    monkeypatch.setattr(alexa_confirmation, "launch_skill", lambda target: False)

    pending.deliver(
        generation,
        "I found a good match. Want me to play it?",
        "media_player.echo",
        lambda text, target=None: notifications.append((text, target)),
    )

    assert notifications == [(alexa_confirmation.RECOVERY, "media_player.echo")]
    assert pending.answer("yes") is None


def test_answer_consumes_pending_and_packages_yes_no_work(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    pending = alexa_confirmation.PendingConfirmation(clock=clock)
    generation = pending.begin("original", "music", "alexa_hermes")
    monkeypatch.setattr(alexa_confirmation, "launch_skill", lambda target: True)
    pending.deliver(generation, "Found it. Want me to play it?", "media_player.echo", lambda *_args, **_kwargs: None)

    work = pending.answer("yes")

    assert work is not None
    assert work["profile"] == "music"
    assert work["session"] == "alexa_hermes"
    assert "Original request: original" in work["text"]
    assert "DJ reply/question: Found it. Want me to play it?" in work["text"]
    assert "User answer: yes" in work["text"]
    assert "do not ask another question" in work["text"]
    assert pending.answer("yes") is None


def test_expired_pending_launch_falls_back_to_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    pending = alexa_confirmation.PendingConfirmation(clock=clock)
    generation = pending.begin("original", "music", "alexa_hermes")
    monkeypatch.setattr(alexa_confirmation, "launch_skill", lambda target: True)
    pending.deliver(generation, "Found it. Want me to play it?", "media_player.echo", lambda *_args, **_kwargs: None)

    clock.advance(31)

    assert pending.launch()["text"] == alexa_confirmation.READY
    assert pending.answer("yes") is None


def test_stale_generation_cannot_deliver_over_newer_pending() -> None:
    clock = Clock()
    pending = alexa_confirmation.PendingConfirmation(clock=clock)
    old = pending.begin("old", "music", "alexa_hermes")
    new = pending.begin("new", "music", "alexa_hermes")
    calls = []

    pending.deliver(old, "Old reply", "media_player.echo", lambda text, target=None: calls.append((text, target)))
    pending.deliver(new, "New reply", "media_player.echo", lambda text, target=None: calls.append((text, target)))

    assert calls == [("New reply", "media_player.echo")]


def test_cancel_can_clear_specific_generation() -> None:
    pending = alexa_confirmation.PendingConfirmation(clock=Clock())
    first = pending.begin("old", "music", "alexa_hermes")
    second = pending.begin("new", "music", "alexa_hermes")

    pending.cancel(first)
    assert pending.pending is not None
    pending.cancel(second)
    assert pending.pending is None


def test_voice_stack_wires_launch_yes_no_and_generation_delivery() -> None:
    source = _source("plugins/voice_stack/__init__.py")
    assert "from . import alexa_confirmation" in source
    assert "_ALEXA_PENDING = alexa_confirmation.PendingConfirmation()" in source
    assert "if text == alexa_confirmation.LAUNCH" in source
    assert 'alexa_confirmation.YES: "yes"' in source
    assert 'alexa_confirmation.NO: "no"' in source
    assert "_ALEXA_PENDING.answer(" in source
    assert "_ALEXA_PENDING.begin(text, profile, agent_session, session_title=agent_title, target_future=target_cf)" in source
    assert "_ALEXA_PENDING.deliver(generation, reply, target, _deliver_slow_answer)" in source


def test_slow_ack_path_passes_generation_to_done_callback() -> None:
    source = _source("plugins/voice_stack/__init__.py")
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == '_schedule_slow_delivery']
    assert any(len(node.args) == 3 for node in calls)
    assert any(len(node.args) == 2 for node in calls)


def test_conversation_component_passes_reprompt_extra_data() -> None:
    source = _source("custom_components/hermes/conversation.py")
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "async_set_speech"]
    assert calls, "conversation integration should call IntentResponse.async_set_speech"
    assert "reprompt = str(result.get(\"reprompt\") or \"\").strip()" in source
    assert 'response.async_set_speech(response_speech, extra_data={"reprompt": reprompt})' in source


def test_docs_include_deploy_and_rollback_warning() -> None:
    doc = _source("docs/alexa-delayed-confirmation.md")
    assert "AMAZON.YesIntent" in doc
    assert "AMAZON.NoIntent" in doc
    assert "reprompt" in doc
    assert "rollback" in doc.lower()
    assert "Physical Echo test" in doc



@pytest.mark.parametrize("reply", [
    COACH_REPLY,
    "Confirm that October third is the date you want imported.",
    "Should I import this run with Qiaodan shoes?",
    "Would you like me to log the run?",
])
def test_coach_yes_no_eligibility(reply):
    assert alexa_confirmation.eligible_confirmation("sportscoach", reply)


@pytest.mark.parametrize("profile,reply", [
    ("sportscoach", "Which shoe did you wear?"),
    ("sportscoach", "Confirm which date is correct."),
    ("sportscoach", "Confirm that which date is correct."),
    ("sportscoach", "Please confirm the date."),
    ("sportscoach", "October third is the date you want imported."),
    ("sportscoach", "Confirmed the run is imported."),
    ("sportscoach", "Confirm October third is correct. Which shoe?"),
    ("sportscoach", "x" * 601 + " Should I import it?"),
    ("default", COACH_REPLY),
    ("coder", "Should I delete the repository?"),
    ("music", "Confirm October third is the date you want imported."),
])
def test_coach_confirmation_rejects_ambiguous_or_unrelated_replies(profile, reply):
    assert not alexa_confirmation.eligible_confirmation(profile, reply)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("answer", ["yes", "no"])
def test_coach_answer_expires_at_exact_deadline(monkeypatch, native, answer):
    clock = Clock()
    pending = alexa_confirmation.PendingConfirmation(clock=clock)
    generation = pending.begin("import run", "sportscoach", "alexa_hermes")
    monkeypatch.setattr(alexa_confirmation, "launch_skill", lambda target: True)
    if native:
        pending.native_response(generation, COACH_REPLY)
    else:
        pending.deliver(generation, COACH_REPLY, "media_player.echo", lambda *a, **kw: None)
    clock.advance(30)
    assert pending.answer(answer) is None
    assert pending.launch()["text"] == alexa_confirmation.READY


def test_native_response_stale_generation_preserves_new_question():
    pending = alexa_confirmation.PendingConfirmation(clock=Clock())
    old = pending.begin("old", "sportscoach", "alexa_hermes")
    new = pending.begin("new", "sportscoach", "alexa_hermes")
    assert pending.native_response(new, COACH_REPLY)["reprompt"]
    assert not pending.native_response(old, COACH_REPLY)["reprompt"]
    work = pending.answer("no")
    assert work is not None
    assert "Original request: new" in work["text"]


def test_native_response_non_confirmation_clears_pending():
    pending = alexa_confirmation.PendingConfirmation(clock=Clock())
    generation = pending.begin("run", "sportscoach", "alexa_hermes")
    assert not pending.native_response(generation, "Run imported.")["reprompt"]
    assert pending.pending is None


def test_coach_new_request_cancels_previous_confirmation():
    pending = alexa_confirmation.PendingConfirmation(clock=Clock())
    generation = pending.begin("old run", "sportscoach", "alexa_hermes")
    pending.native_response(generation, COACH_REPLY)
    pending.begin("new request", "sportscoach", "alexa_hermes")
    assert pending.answer("yes") is None


def test_native_response_expired_work_does_not_rearm():
    clock = Clock()
    pending = alexa_confirmation.PendingConfirmation(clock=clock)
    generation = pending.begin("run", "sportscoach", "alexa_hermes")
    clock.advance(180)
    assert not pending.native_response(generation, COACH_REPLY)["reprompt"]
    assert pending.answer("yes") is None



@pytest.mark.parametrize("target", ["", "media_player.echo"])
def test_coach_failed_relaunch_clears_confirmation(monkeypatch, target):
    pending = alexa_confirmation.PendingConfirmation(clock=Clock())
    generation = pending.begin("run", "sportscoach", "alexa_hermes")
    notifications = []
    monkeypatch.setattr(alexa_confirmation, "launch_skill", lambda target: False)
    pending.deliver(generation, COACH_REPLY, target, lambda text, target=None: notifications.append(text))
    assert notifications == [alexa_confirmation.RECOVERY]
    assert pending.answer("yes") is None


def test_documented_query_and_launch_handlers_propagate_native_reprompt():
    import yaml
    doc = _source("docs/alexa-delayed-confirmation.md")
    config = yaml.safe_load(doc.split("```yaml\n", 1)[1].split("```", 1)[0])["intent_script"]
    for name in ("HermesIntent", alexa_confirmation.SKILL_ID):
        assert "extra_data.reprompt" in config[name]["reprompt"]["text"]
        data = config[name]["action"][0]["data"]
        assert data["agent_id"] == "conversation.hermes"
        assert data["conversation_id"] == "alexa_hermes"
    for name, marker in (("AMAZON.YesIntent", alexa_confirmation.YES), ("AMAZON.NoIntent", alexa_confirmation.NO)):
        assert config[name]["action"][0]["data"]["text"] == marker
        assert "reprompt" not in config[name]  # consumed answer ends native dialogue

@pytest.mark.parametrize("reply", [
    "Did you mean the Qiaodan Leili 2.0, yes or no?",
    "Confirm Qiaodan Leili 2.0 is the shoe.",
    "Should I use model 2.0?",
])
def test_decimal_versions_preserve_binary_question_eligibility(reply):
    assert alexa_confirmation.eligible_confirmation("sportscoach", reply)


@pytest.mark.parametrize("reply", [
    "Music started, but I couldn’t verify the playlist—is the café jazz coming through?",
    "I sent it. Did the music start?",
    "The player still reports Chill Jazz. Did the French track start?",
    "Did that song start?",
    "The player reports music. Can you hear it?",
    "Is the selection right?",
])
@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("answer", ["yes", "no"])
def test_playback_report_keeps_native_answer_and_original_session(monkeypatch, reply, native, answer):
    pending = alexa_confirmation.PendingConfirmation(clock=Clock())
    generation = pending.begin("play French jazz", "music", "original-dj-session")
    launches, notifications = [], []
    monkeypatch.setattr(alexa_confirmation, "launch_skill", lambda target: launches.append(target) or True)
    if native:
        assert pending.native_response(generation, reply)["reprompt"]
    else:
        pending.deliver(generation, reply, "media_player.echo", lambda *a, **k: notifications.append(a))
        assert launches == ["media_player.echo"]
        assert notifications == []
        assert pending.launch()["reprompt"]
    work = pending.answer(answer)
    assert work["session"] == "original-dj-session"
    assert f"User answer: {answer}" in work["text"]
    assert reply in work["text"]
    assert pending.answer(answer) is None


@pytest.mark.parametrize("reply", [
    "Which music is playing?",
    "What is coming through?",
    "Did the music start? Which song?",
    "Which playlist? Is it playing?",
    "Music is playing.",
])
def test_playback_report_does_not_arm_open_questions(reply):
    assert not alexa_confirmation.eligible_confirmation("music", reply)

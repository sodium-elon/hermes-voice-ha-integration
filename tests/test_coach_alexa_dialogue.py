"""Exercise the existing Alexa handler with fake agents and no device I/O."""
import asyncio
import concurrent.futures
import threading
from types import SimpleNamespace

import pytest

from plugins import voice_stack as voice
from plugins.voice_stack import alexa_confirmation

COACH_REPLY = "Found the run. Confirm October third is the date you want imported."
LIVE_IDENTITY_REPLY = (
    'John, did you mean log your October eighth run with the Saucony Kinvara 16? '
    'The voice transcript says “kindergarten,” and we’re logging shoes, not enrolling recruits.'
)


@pytest.fixture
def dialogue(monkeypatch):
    pending = alexa_confirmation.PendingConfirmation()
    calls = []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(voice, "_ALEXA_PENDING", pending)
    monkeypatch.setattr(voice, "_ALEXA_TURN_LOCK", threading.Lock())
    monkeypatch.setattr(voice, "ALEXA_EXECUTOR", executor)
    monkeypatch.setattr(voice, "_resolve_last_called_media_player", lambda: "media_player.echo")
    monkeypatch.setattr(voice, "_route_assist_profile", lambda text: "sportscoach")
    monkeypatch.setattr(voice, "_schedule_assist_label", lambda text: None)
    monkeypatch.setattr(voice.events, "emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(voice, "ALEXA_AGENT_BUDGET_SECONDS", 1)

    def agent(text, **kwargs):
        calls.append((text, kwargs))
        return COACH_REPLY if len(calls) == 1 else "The answer was handled."

    monkeypatch.setattr(voice, "_run_full_agent", agent)
    yield pending, calls
    executor.shutdown(wait=True)


def ask(text):
    return asyncio.run(voice._handle_assist_query_with_llm(
        SimpleNamespace(), {"text": text, "conversation_id": "alexa_hermes"}
    ))


def test_new_run_request_uses_fresh_agent_history(dialogue):
    _, calls = dialogue
    yesterday = 'here is for running coach: 2026-10-07 Nike Vomero Plus'
    today = 'here is for running coach: 2026-10-08 kinabaro 16'
    ask(yesterday)
    ask(today)
    assert calls[0][0] == yesterday
    assert calls[1][0] == today
    assert calls[0][1]['session'] != calls[1][1]['session']
    assert calls[1][1]['session'] != voice.ALEXA_SESSION_ID
    assert calls[0][1]['session_title'] != calls[1][1]['session_title']


@pytest.mark.parametrize('question', [
    'Did you mean which shoe? The transcript was unclear.',
    'Did you mean the Saucony Kinvara 16? Which run should I log?',
    'Which run should I log? Did you mean the Saucony Kinvara 16? The transcript was unclear.',
])
def test_identity_question_does_not_arm_open_or_multiple_questions(question):
    assert not alexa_confirmation.eligible_confirmation('sportscoach', question)


@pytest.mark.parametrize('question', [
    'For October eighth, did you mean the Saucony Kinvara 16?',
    LIVE_IDENTITY_REPLY,
])
def test_shoe_identity_question_keeps_current_request_context(dialogue, monkeypatch, question):
    _, calls = dialogue
    request = 'here is for running coach: 2026-10-08 kinabaro 16'
    def agent(text, **kwargs):
        calls.append((text, kwargs))
        return question if len(calls) == 1 else 'Answer handled.'
    monkeypatch.setattr(voice, '_run_full_agent', agent)
    result = ask(request)
    assert result['reprompt'] == alexa_confirmation.ANSWER_REPROMPT
    monkeypatch.setattr(voice, '_route_assist_profile', lambda _: pytest.fail('Do not route an answer as a new task'))
    assert ask(alexa_confirmation.YES)['text'] == 'Answer handled.'
    assert calls[1][1] == calls[0][1]
    assert request in calls[1][0] and question in calls[1][0]
    assert '2026-10-07' not in calls[1][0]
    assert ask(alexa_confirmation.YES)['text'] == alexa_confirmation.NO_PENDING
    assert len(calls) == 2


@pytest.mark.parametrize('question', ['Did you mean the Saucony Kinvara 16?', LIVE_IDENTITY_REPLY])
def test_slow_confirmation_reuses_origin_echo(dialogue, monkeypatch, question):
    _, calls = dialogue
    finish, launched = threading.Event(), threading.Event()
    targets = []
    def lookup():
        targets.append('lookup')
        return 'media_player.original_echo' if len(targets) == 1 else 'media_player.other_echo'
    def agent(text, **kwargs):
        calls.append((text, kwargs))
        if len(calls) == 1:
            assert finish.wait(2)
            return question
        return 'Answer handled.'
    monkeypatch.setattr(voice, '_resolve_last_called_media_player', lookup)
    monkeypatch.setattr(voice, '_run_full_agent', agent)
    monkeypatch.setattr(voice, 'ALEXA_AGENT_BUDGET_SECONDS', 0.01)
    monkeypatch.setattr(alexa_confirmation, 'launch_skill', lambda target: launched.set() or target == 'media_player.original_echo')
    try:
        assert ask('here is for running coach: 2026-10-08 kinabaro 16')['slow']
    finally:
        finish.set()
    assert launched.wait(2)
    assert ask(alexa_confirmation.LAUNCH)['reprompt'] == alexa_confirmation.ANSWER_REPROMPT
    monkeypatch.setattr(voice, 'ALEXA_AGENT_BUDGET_SECONDS', 1)
    assert ask(alexa_confirmation.YES)['text'] == 'Answer handled.'
    assert targets == ['lookup']
    assert calls[1][1]['session'] == calls[0][1]['session']


@pytest.mark.parametrize('answer', ['yes', 'no', '__hermes_alexa_yes__', '__hermes_alexa_no__'])
def test_answer_from_ha_query_route_consumes_only_current_question(dialogue, monkeypatch, answer):
    _, calls = dialogue
    ask('here is for running coach: 2026-10-08 kinabaro 16')
    monkeypatch.setattr(voice, '_route_assist_profile', lambda _: pytest.fail('An answer must not become a new task'))
    result = asyncio.run(voice._handle_assist_query_with_llm(SimpleNamespace(), {'text': answer, 'conversation_id': 'amzn1.echo-api.session.current'}))
    assert result['conversation_id'] == 'amzn1.echo-api.session.current'
    assert result['text'] == 'The answer was handled.'
    assert calls[1][1] == calls[0][1]
    assert '2026-10-08 kinabaro 16' in calls[1][0]
    assert 'User answer: ' + ('yes' if answer.endswith('yes__') or answer == 'yes' else 'no') in calls[1][0]
    assert not result.get('reprompt')


def test_fast_confirmation_retains_original_target_future(dialogue, monkeypatch):
    _, _ = dialogue
    targets = []
    monkeypatch.setattr(voice, '_resolve_last_called_media_player', lambda: targets.append('original lookup') or 'media_player.original_echo')
    assert ask('here is for running coach: 2026-10-08 kinabaro 16')['reprompt']
    assert ask(alexa_confirmation.YES)['text'] == 'The answer was handled.'
    assert targets == ['original lookup']


@pytest.mark.parametrize('answer', ['yes', 'no', '__hermes_alexa_yes__', '__hermes_alexa_no__'])
def test_unprompted_answer_does_not_launch_agent(dialogue, monkeypatch, answer):
    _, calls = dialogue
    monkeypatch.setattr(voice, '_route_assist_profile', lambda _: pytest.fail('Unprompted answer must not route'))
    assert ask(answer)['text'] == alexa_confirmation.NO_PENDING
    assert calls == []


def test_expired_answer_does_not_revive_request(dialogue):
    pending, calls = dialogue
    now = [1000.0]
    pending.clock = lambda: now[0]
    assert ask('here is for running coach: 2026-10-08 kinabaro 16')['reprompt']
    now[0] += 30
    assert ask('yes')['text'] == alexa_confirmation.NO_PENDING
    assert len(calls) == 1


def test_fast_coach_question_keeps_native_skill_listening(dialogue):
    pending, calls = dialogue
    reply = ask("import today's run with Qiaodan shoes")
    assert reply["text"] == COACH_REPLY
    assert reply.get("reprompt") == alexa_confirmation.ANSWER_REPROMPT

    # Bare confirmation must return to the saved specialist, not be re-routed.
    answer = ask(alexa_confirmation.YES)
    assert answer["text"] == "The answer was handled."
    assert not answer.get("reprompt")
    assert calls[1][1]["profile"] == "sportscoach"
    assert calls[1][1]["session"] == calls[0][1]["session"]
    assert calls[1][1]["session_title"] == calls[0][1]["session_title"]
    assert "Original request: import today's run with Qiaodan shoes" in calls[1][0]
    assert f"Coach reply/question: {COACH_REPLY}" in calls[1][0]
    assert "User answer: yes" in calls[1][0]
    assert pending.answer("yes") is None
    assert ask(alexa_confirmation.YES)["text"] == alexa_confirmation.NO_PENDING
    assert len(calls) == 2



@pytest.mark.parametrize("marker,answer", [(alexa_confirmation.YES, "yes"), (alexa_confirmation.NO, "no")])
def test_slow_coach_question_and_single_answer_use_existing_handler(dialogue, monkeypatch, marker, answer):
    pending, calls = dialogue
    finish, launched = threading.Event(), threading.Event()
    def agent(text, **kwargs):
        calls.append((text, kwargs))
        if len(calls) == 1:
            assert finish.wait(2), "test did not release the slow agent"
        return COACH_REPLY  # even another question must not re-arm after an answer
    monkeypatch.setattr(voice, "_run_full_agent", agent)
    monkeypatch.setattr(voice, "ALEXA_AGENT_BUDGET_SECONDS", 0.01)
    monkeypatch.setattr(alexa_confirmation, "launch_skill", lambda target: launched.set() or True)
    monkeypatch.setattr(voice, "_deliver_slow_answer", lambda *a, **kw: pytest.fail("Unexpected notification"))

    try:
        reply = ask("import today's run")
        assert reply["slow"] is True
    finally:
        finish.set()
    assert launched.wait(2), "existing skill was not relaunched"
    assert ask(alexa_confirmation.LAUNCH) == {
        "ok": True, "conversation_id": "alexa_hermes",
        "text": COACH_REPLY, "reprompt": alexa_confirmation.ANSWER_REPROMPT,
    }
    monkeypatch.setattr(voice, "_route_assist_profile", lambda text: pytest.fail("Answer was reclassified"))
    monkeypatch.setattr(voice, "ALEXA_AGENT_BUDGET_SECONDS", 1)
    reply = ask(marker)
    assert not reply.get("reprompt")
    assert calls[1][1]["profile"] == "sportscoach"
    assert calls[1][1]["session"] == calls[0][1]["session"]
    assert calls[1][1]["session_title"] == calls[0][1]["session_title"]
    assert f"User answer: {answer}" in calls[1][0]
    assert pending.answer(answer) is None
    assert ask(marker)["text"] == alexa_confirmation.NO_PENDING
    assert len(calls) == 2




@pytest.mark.parametrize('follow_up', [False, True], ids=['new-request', 'confirmation-answer'])
def test_cancelled_handler_keeps_worker_owned_until_single_completion(dialogue, monkeypatch, follow_up):
    pending, calls = dialogue
    started, finish = threading.Event(), threading.Event()
    delivered, released = threading.Event(), threading.Event()
    notifications, releases = [], []
    completion = 'Your run has been saved with the requested shoes.'
    request = 'import today\'s run'
    if follow_up:
        generation = pending.begin(request, 'sportscoach', 'saved-coach-session')
        pending.native_response(generation, COACH_REPLY)
        request = alexa_confirmation.YES

    def agent(text, **kwargs):
        calls.append((text, kwargs))
        if len(calls) == 1:
            started.set()
            assert finish.wait(5), 'test did not release the cancelled handler worker'
            return completion
        return 'Fresh request handled.'

    def tts(text, target):
        notifications.append((text, target))
        delivered.set()
        return True

    original_release = voice._release_alexa_turn
    def release():
        releases.append('release')
        original_release()
        released.set()

    monkeypatch.setattr(voice, '_run_full_agent', agent)
    monkeypatch.setattr(voice, '_release_alexa_turn', release)
    monkeypatch.setattr(voice, '_emit_assist_response_label', lambda *a, **kw: None)
    monkeypatch.setattr(voice, 'ALEXA_AGENT_BUDGET_SECONDS', 30)
    from plugins.voice_stack import pipeline
    monkeypatch.setattr(pipeline, 'play_text_alexa', tts)
    monkeypatch.setattr(alexa_confirmation, 'launch_skill', lambda _: pytest.fail('Completion must not reopen the microphone'))

    async def scenario():
        first = asyncio.create_task(voice._handle_assist_query_with_llm(
            SimpleNamespace(), {'text': request, 'conversation_id': 'alexa_hermes'}))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = await voice._handle_assist_query_with_llm(
                SimpleNamespace(), {'text': 'show my training summary', 'conversation_id': 'alexa_hermes'})
            assert second.get('busy') is True
            assert second['text'] == voice.ALEXA_BUSY
            assert len(calls) == 1
            assert releases == []
            assert notifications == []
            assert voice._ALEXA_TURN_LOCK.locked()
        finally:
            finish.set()
        assert await asyncio.to_thread(delivered.wait, 2), 'cancelled worker completion was lost'
        assert await asyncio.to_thread(released.wait, 2), 'completed worker ownership was not released'
        assert notifications == [(completion, 'media_player.echo')]
        assert releases == ['release']
        assert not voice._ALEXA_TURN_LOCK.locked()
        fresh = await voice._handle_assist_query_with_llm(
            SimpleNamespace(), {'text': 'new training request', 'conversation_id': 'alexa_hermes'})
        assert fresh['text'] == 'Fresh request handled.'
        assert not fresh.get('busy')
        assert len(calls) == 2
        assert releases == ['release', 'release']  # exactly one release per admitted turn
        assert notifications == [(completion, 'media_player.echo')]

    asyncio.run(scenario())


def test_new_query_cancels_coach_confirmation_through_handler(dialogue):
    pending, calls = dialogue
    assert ask("import today's run").get("reprompt")
    assert not ask("show my training summary").get("reprompt")
    assert ask(alexa_confirmation.YES)["text"] == alexa_confirmation.NO_PENDING
    assert len(calls) == 2
    assert pending.pending is None

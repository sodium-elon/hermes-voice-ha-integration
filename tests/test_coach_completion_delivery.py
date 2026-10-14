"""Completed results outlive a listening window; no device or network I/O."""
from plugins.voice_stack.alexa_confirmation import PendingConfirmation


def test_slow_coach_completion_is_delivered_once_after_question_timer_expired():
    now = [0.0]
    pending = PendingConfirmation(clock=lambda: now[0])
    generation = pending.begin('log October 7 run', 'sportscoach', 'alexa_hermes')
    now[0] = 377.0
    delivered = []
    def notify(reply, *, target):
        delivered.append((reply, target))
    reply = 'Your October seventh run has been added with the Nike Vomero Plus.'
    pending.deliver(generation, reply, 'media_player.asking_echo', notify)
    assert delivered == [(reply, 'media_player.asking_echo')]
    pending.deliver(generation, reply, 'media_player.asking_echo', notify)
    assert len(delivered) == 1
    assert pending.answer('yes') is None


def test_expired_coach_question_does_not_extend_authorization_or_listening():
    now = [0.0]
    pending = PendingConfirmation(clock=lambda: now[0])
    generation = pending.begin('log my run', 'sportscoach', 'alexa_hermes')
    now[0] = 377.0
    delivered = []
    pending.deliver(generation, 'Would you like me to import this run?', 'media_player.echo', lambda *a, **k: delivered.append(a))
    assert delivered == []
    assert pending.answer('yes') is None


def test_new_request_supersedes_old_coach_completion():
    now = [0.0]
    pending = PendingConfirmation(clock=lambda: now[0])
    old = pending.begin('log my run', 'sportscoach', 'alexa_hermes')
    new = pending.begin('new request', 'sportscoach', 'alexa_hermes')
    now[0] = 377.0
    delivered = []
    notify = lambda reply, **kwargs: delivered.append(reply)
    pending.deliver(old, 'Old run saved.', 'media_player.echo', notify)
    assert delivered == []
    pending.deliver(new, 'New request complete.', 'media_player.echo', notify)
    assert delivered == ['New request complete.']


def test_dj_expiry_is_unchanged():
    now = [0.0]
    pending = PendingConfirmation(clock=lambda: now[0])
    generation = pending.begin('play music', 'music', 'alexa_hermes')
    now[0] = 377.0
    delivered = []
    pending.deliver(generation, 'Done.', 'media_player.echo', lambda *a, **k: delivered.append(a))
    assert delivered == []

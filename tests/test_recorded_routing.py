"""Post-chime recording boundary; all dispatch and judgment calls isolated."""
import json
from types import SimpleNamespace
import pytest
from plugins import voice_stack as voice
from plugins.voice_stack import ha_conversation, dj_audio


@pytest.fixture
def setup_route(monkeypatch, tmp_path):
    wav = tmp_path / 'request.wav'
    wav.write_bytes(b'RIFF-test')
    calls = []
    def forbidden(*a, **kw):
        raise AssertionError('legacy music path entered')
    monkeypatch.setattr(voice, '_jev_music_gate', forbidden)
    monkeypatch.setattr(voice, '_resolve_music_request', forbidden)
    monkeypatch.setattr(voice, '_alexa_playback_return', forbidden)
    monkeypatch.setattr(voice.events, 'emit', lambda *a, **kw: None)
    monkeypatch.setattr(ha_conversation, 'process_conversation', lambda *a, **kw: {
        'response': {'response_type': 'error', 'data': {'code': 'no_intent_match'}}})
    monkeypatch.setattr(ha_conversation, 'run_live_channel', lambda *a, **kw: {'ok': False})
    monkeypatch.setattr(voice, '_complete_voice_with_hermes', lambda ctx, prompt, **kw: (
        calls.append((prompt, kw)), 'agent reply')[1])
    return wav, calls


def fake_decider(monkeypatch, route, observed):
    def decide(hypotheses):
        observed.extend(hypotheses)
        return {'route': route, 'probability': .9 if route == 'music' else .1,
                'hypotheses': hypotheses, 'artist_hints': []}
    monkeypatch.setattr(voice, 'music_handoff', SimpleNamespace(
        decide=decide, build_prompt=lambda path, state: json.dumps({'audio_path': path, **state})
    ), raising=False)


def test_recorded_music_hands_off_original_and_alternate(monkeypatch, setup_route):
    wav, calls = setup_route
    observed = []
    fake_decider(monkeypatch, 'music', observed)
    monkeypatch.setattr(dj_audio, 'transcribe_recording', lambda path: 'Face the beaters.')
    assert voice._route_voice_transcript(object(), 'Thanks for watching.',
        confidence=.087, audio_path=str(wav)) == 'agent reply'
    assert [x['text'] for x in observed] == ['Thanks for watching.', 'Face the beaters.']
    assert observed[0]['confidence'] == .087
    payload = json.loads(calls[0][0])
    assert payload['audio_path'] == str(wav)
    assert calls[0][1] == {'profile': 'music'}


def test_recorded_nonmusic_cannot_reenter_legacy_music_after_ha_miss(monkeypatch, setup_route):
    wav, calls = setup_route
    fake_decider(monkeypatch, 'nonmusic', [])
    assert voice._route_voice_transcript(object(), 'Play BBC News on television',
        confidence=.9, audio_path=str(wav)) == 'Home Assistant could not find a matching device.'
    assert calls == []


def test_failed_second_decode_does_not_erase_original(monkeypatch, setup_route):
    wav, calls = setup_route
    observed = []
    fake_decider(monkeypatch, 'music', observed)
    def fail(path):
        raise RuntimeError('decoder unavailable')
    monkeypatch.setattr(dj_audio, 'transcribe_recording', fail)
    assert voice._route_voice_transcript(object(), 'Play the Beatles',
        confidence=.1, audio_path=str(wav)) == 'agent reply'
    assert [h['text'] for h in observed] == ['Play the Beatles']
    assert calls[0][1] == {'profile': 'music'}


def test_recorded_jev_outage_never_calls_other_handlers(monkeypatch, setup_route):
    from plugins.voice_stack import music
    wav, calls = setup_route
    def fail(hypotheses):
        raise music.TypeSafeUnavailable(music.UNAVAILABLE)
    monkeypatch.setattr(voice, 'music_handoff', SimpleNamespace(decide=fail), raising=False)
    assert voice._route_voice_transcript(object(), 'Play the Beatles',
        audio_path=str(wav)) == music.UNAVAILABLE
    assert calls == []


def test_uncertain_recording_never_dispatches_or_calls_general_agent(monkeypatch, setup_route):
    wav, calls = setup_route
    fake_decider(monkeypatch, 'uncertain', [])
    assert 'repeat' in voice._route_voice_transcript(object(), 'unintelligible words',
        confidence=.9, audio_path=str(wav)).lower()
    assert calls == []

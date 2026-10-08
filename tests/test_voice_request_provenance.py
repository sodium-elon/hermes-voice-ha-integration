"""Voice receipt provenance at the real Sessions API serialization boundary."""
import json
from pathlib import Path
from uuid import UUID

from plugins.voice_stack import sessions_api


class Response:
    status_code = 200
    encoding = 'utf-8'
    def iter_lines(self, **kwargs):
        yield 'event: assistant.completed'
        yield 'data: {"content":"Received."}'
    def close(self):
        pass


def test_sportscoach_receives_unique_durable_ids_without_spoken_ids(tmp_path, monkeypatch):
    import requests
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('API_SERVER_KEY', 'fixture-key')
    monkeypatch.setattr(sessions_api, 'ensure_session', lambda **kwargs: 'alexa_hermes')
    text = 'here is for running coach: 2026-10-07 nike vomero plus'
    ids = []

    def post(url, **kwargs):
        body = kwargs['json']
        original, metadata = body['input'].split('\n\n[Voice transport receipt]\n')
        assert original == text
        envelope = json.loads(metadata)
        request_id = envelope['source_message_id']
        assert str(UUID(request_id.removeprefix('voice:'))) == request_id.removeprefix('voice:')
        receipt_path = Path(envelope['receipt_path'])
        assert receipt_path.parent == tmp_path / 'voice' / 'requests'
        receipt = json.loads(receipt_path.read_text())
        assert receipt['source_message_id'] == request_id
        assert receipt['text'] == text
        assert receipt['profile'] == 'sportscoach'
        assert receipt['session'] == 'alexa_hermes'
        assert receipt['received_at']
        assert receipt_path.stat().st_mode & 0o777 == 0o600
        assert 'source-message-id' in body['system_message']
        assert 'does not authorize' in body['system_message']
        ids.append(request_id)
        return Response()

    monkeypatch.setattr(requests, 'post', post)
    for _ in range(2):
        assert sessions_api.complete(text, profile='sportscoach') == 'Received.'
    assert len(set(ids)) == 2


def test_existing_prompt_is_preserved_and_provenance_prompt_is_stable(tmp_path, monkeypatch):
    import requests
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('API_SERVER_KEY', 'fixture-key')
    monkeypatch.setattr(sessions_api, 'ensure_session', lambda **kwargs: 'voice-stack')
    bodies = []
    def post(url, **kwargs):
        bodies.append(kwargs['json'])
        return Response()
    monkeypatch.setattr(requests, 'post', post)
    for text in ['log my run', 'what is my pace?']:
        sessions_api.complete(text, profile='sportscoach', system_prompt='Existing voice rules.')
    assert bodies[0]['system_message'] == bodies[1]['system_message']
    assert bodies[0]['system_message'].startswith('Existing voice rules.\n\n')
    sessions_api.complete('play music', profile='music', system_prompt='Music rules.')
    assert bodies[-1] == {'input': 'play music', 'system_message': 'Music rules.'}
    assert len(list((tmp_path / 'voice' / 'requests').glob('*.json'))) == 2


def test_archive_failure_blocks_forwarding(tmp_path, monkeypatch):
    import pytest
    import requests
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('API_SERVER_KEY', 'fixture-key')
    monkeypatch.setattr(sessions_api, 'ensure_session', lambda **kwargs: 'alexa_hermes')
    (tmp_path / 'voice').write_text('not a directory')
    monkeypatch.setattr(requests, 'post', lambda *args, **kwargs: pytest.fail('Must not send unaudited request'))
    with pytest.raises(sessions_api.SessionsAPIUnavailable, match='provenance could not be archived'):
        sessions_api.complete('log my run', profile='sportscoach')

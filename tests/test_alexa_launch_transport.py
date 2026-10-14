"""Skill relaunch must work inside an isolated voice plugin; no real HTTP."""
import builtins
import json
import urllib.request

import pytest

from plugins.voice_stack import alexa_confirmation, ha_conversation


@pytest.fixture(autouse=True)
def no_devices(monkeypatch):
    monkeypatch.setattr(ha_conversation, '_get_config', lambda: ('http://ha.fixture:8123', 'fixture-token'))
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('Unexpected real HTTP'))
    original = builtins.__import__
    def isolated(name, *args, **kwargs):
        if 'home_assistant' in name:
            raise ImportError('Sibling plugin unavailable in isolated gateway namespace')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', isolated)


def test_relaunch_uses_owned_ha_client_not_disabled_sibling(monkeypatch):
    seen = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'[]'
    def post(request, timeout):
        seen.append((request, timeout))
        return Response()
    monkeypatch.setattr(urllib.request, 'urlopen', post)
    assert alexa_confirmation.launch_skill('media_player.asking_echo') is True
    request, timeout = seen[0]
    assert request.full_url == 'http://ha.fixture:8123/api/services/media_player/play_media'
    assert request.get_method() == 'POST'
    assert request.get_header('Authorization') == 'Bearer fixture-token'
    assert json.loads(request.data) == {'entity_id': 'media_player.asking_echo', 'media_content_type': 'skill', 'media_content_id': alexa_confirmation.SKILL_ID}
    assert timeout <= 5


def test_no_token_is_fail_closed(monkeypatch):
    monkeypatch.setattr(ha_conversation, '_get_config', lambda: ('http://ha.fixture:8123', ''))
    assert alexa_confirmation.launch_skill('media_player.asking_echo') is False


def test_http_failure_is_not_launch_success(monkeypatch):
    def fail(*a, **k): raise OSError('fixture HA failure')
    monkeypatch.setattr(urllib.request, 'urlopen', fail)
    assert alexa_confirmation.launch_skill('media_player.asking_echo') is False


def test_no_target_does_not_dispatch():
    assert alexa_confirmation.launch_skill('') is False

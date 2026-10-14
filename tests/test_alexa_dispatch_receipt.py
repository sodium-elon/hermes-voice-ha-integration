"""HA transport acknowledgments are acceptance, never audible playback proof."""
import json
import pytest
from plugins.voice_stack import ha_conversation as ha

class Response:
    def __init__(self, raw): self.raw = raw
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def raise_for_status(self): pass
    async def read(self): return self.raw
    async def json(self): return json.loads(self.raw)

@pytest.fixture
def transport(monkeypatch):
    import aiohttp
    calls=[]
    def install(raw):
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            def post(self,url,**kwargs):
                calls.append((url,kwargs));return Response(raw)
        monkeypatch.setattr(aiohttp,'ClientSession',Session)
        monkeypatch.setattr(ha,'_get_config',lambda:('http://ha.test','fixture-not-real'))
        return calls
    return install

@pytest.mark.parametrize('raw',[b'',b'not-json',b'null',b'{}',b'{"error":"dispatch failed"}',b'{"success":true,"affected_entities":[]}',b'[{"entity_id":""}]',b'[{"entity_id":"garbage"}]',b'[{"entity_id":"media_player.test","state":{},"attributes":[]}]'])
def test_invalid_service_ack_does_not_create_dispatch_receipt(transport,raw):
    transport(raw)
    result=ha.play_alexa_media('Play jazz music on Apple Music','media_player.test_echo')
    assert result['ok'] is False
    assert result['acceptance_kind']=='ha_failed'


@pytest.mark.parametrize('raw', [b'[]', b'[{"entity_id":"media_player.test_echo","state":"idle","attributes":{}}]'])
def test_valid_state_list_is_acceptance_not_playback(transport, raw):
    calls=transport(raw)
    result=ha.play_alexa_media('Play jazz music on Apple Music','media_player.test_echo')
    assert result['ok'] is True
    assert result['acceptance_kind']=='ha_accepted_unverified'
    assert result['command']=='Play jazz music on Apple Music'
    assert calls[0][1]['json']['media_content_type']=='custom'
    assert calls[0][1]['json']['entity_id']=='media_player.test_echo'

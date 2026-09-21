from plugins.voice_stack import music


def test_jev_route_precedes_any_lookup(monkeypatch):
    calls = []
    def select(*args, **kwargs):
        calls.append('route')
        return {'operation': 'genre', 'genre': 'jazz', 'period': '2026', 'confidence': .95}
    monkeypatch.setattr(music, 'select_route', select, raising=False)
    monkeypatch.setattr(music, '_catalog', lambda **kw: calls.append('catalog'))
    monkeypatch.setattr(music, '_build_music_redecode_hotwords', lambda *a, **k: calls.append('hotwords'))
    result = music.resolve(None, 'Play jazz music from 2026', confidence=.9)
    assert calls == ['route']
    assert result['command'] == 'Play jazz music from 2026 on Apple Music'

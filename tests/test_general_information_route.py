"""General Choice must reach Ria without HA/music inference."""
from plugins import voice_stack as voice

def test_general_information_choice_goes_directly_to_default_agent(monkeypatch, tmp_path):
    calls = []
    def forbidden(*a, **kw):
        raise AssertionError("General question must not enter device or music handling")
    monkeypatch.setattr(voice, "_run_full_agent", lambda text, **kw: calls.append((text, kw)) or "Tokyo, depending on the definition.")
    from plugins.voice_stack import ha_conversation, music_handoff
    monkeypatch.setattr(ha_conversation, "process_conversation", forbidden)
    monkeypatch.setattr(music_handoff, "decide", forbidden)
    wav = tmp_path / "original.wav"
    wav.write_bytes(b"fixture")
    result = voice._route_voice_transcript(None, "What's the biggest city in Asia?", confidence=.614,
                                           audio_path=str(wav), request_choice="general_information")
    assert result == "Tokyo, depending on the definition."
    assert calls == [("What's the biggest city in Asia?", {})]

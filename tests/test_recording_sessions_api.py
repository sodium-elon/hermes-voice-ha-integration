"""Recorded DJ turns must not replay expired recordings from shared history."""
import json
import wave

import pytest
import requests

from plugins.voice_stack import events, music_handoff, sessions_api


class Response:
    status_code = 200
    text = ""
    encoding = "utf-8"

    def __init__(self, lines=()):
        self.lines = lines
        self.closed = False

    def iter_lines(self, **kwargs):
        yield from self.lines

    def close(self):
        self.closed = True


def stream(*items):
    for name, payload in items:
        yield f"event: {name}"
        yield "data: " + json.dumps(payload)
        yield ""


@pytest.fixture
def recording(monkeypatch, tmp_path):
    monkeypatch.setenv("API_SERVER_KEY", "test-key")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(events, "emit", lambda *a, **k: None)
    path = tmp_path / "current.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * 160)
    return path


def prompt(path):
    return music_handoff.build_prompt(str(path), {
        "route": "music", "probability": 0.93,
        "hypotheses": [{"text": "Play Swedish music", "source": "initial_stt"}],
    })


@pytest.mark.parametrize("explicit_session", [None, "voice-stack"])
def test_recorded_turn_does_not_replay_pending_wavs(recording, monkeypatch, explicit_session):
    # Reproduce a reused session whose four expired inputs have no assistant
    # boundary, as in incident message 3390. No production state is touched.
    old_names = ["tmpvk2y8ite", "tmpjevc4g_m", "tmpk1zxv3ma", "tmpsaqbz8ye"]
    old_inputs = [prompt(recording.parent / (name + ".wav")) for name in old_names]
    histories = {"voice-stack": old_inputs}
    heard = []
    sessions = []
    responses = []

    def post(url, **kwargs):
        body = kwargs["json"]
        if url.endswith("/api/sessions"):
            sid = body["id"]
            sessions.append(sid)
            response = Response()
            if sid in histories:
                response.status_code = 409
            else:
                histories[sid] = []
            return response
        sid = url.split("/api/sessions/", 1)[1].split("/chat/stream")[0]
        assert kwargs["stream"] is True
        # The receiver loads history; consecutive pending user inputs are
        # merged before inference. Capture the actual request seen by DJ.
        heard.append("\n\n".join(histories[sid] + [body["input"]]))
        histories[sid].append(body["input"])
        def lines():
            # Simulated DJ consumes the shared WAV before callback returns.
            assert recording.exists()
            with wave.open(str(recording), "rb") as audio:
                assert audio.getnframes() == 160
            yield from stream(("assistant.completed", {"content": "Done."}),
                              ("run.completed", {"completed": True}))
        response = Response(lines())
        responses.append(response)
        return response

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setenv("HERMES_VOICE_SESSION_ID", "voice-stack")
    for _ in range(2):
        assert sessions_api.complete(prompt(recording), profile="music",
                                     session=explicit_session) == "Done."
    assert heard == [prompt(recording), prompt(recording)]
    assert len(set(sessions)) == 2
    assert "voice-stack" not in sessions
    assert histories["voice-stack"] == old_inputs  # Never delete/reset history.
    assert all(response.closed for response in responses)


@pytest.mark.parametrize("tail", [[], [("done", {})],
                                    [("run.completed", {"completed": False})]])
def test_recorded_turn_requires_successful_run_completion(recording, monkeypatch, tail):
    response = Response(stream(("assistant.completed", {"content": "Still working."}), *tail))
    monkeypatch.setattr(sessions_api, "ensure_session", lambda **k: k["name"])
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    with pytest.raises(sessions_api.SessionsAPIUnavailable, match="before.*completed"):
        sessions_api.complete(prompt(recording), profile="music")
    assert response.closed


def test_missing_recording_is_rejected_before_transport(recording, monkeypatch):
    recording.unlink()
    def forbidden(*args, **kwargs):
        pytest.fail("Missing audio must not be queued at the gateway")
    monkeypatch.setattr(requests, "post", forbidden)
    with pytest.raises(sessions_api.SessionsAPIUnavailable, match="Recording.*available"):
        sessions_api.complete(prompt(recording), profile="music")


@pytest.mark.parametrize("text", ["Play jazz", "[]", "null", "{broken", "{}",
    '{"role":"DJ Yakkuza","audio_path":"old.wav","routing":null}',
    '{"role":"DJ Yakkuza","audio_path":"","routing":{"route":"music"}}',
    '{"role":"DJ Yakkuza","audio_path":"old.wav","routing":{"route":"nonmusic"}}'])
def test_ordinary_music_chat_preserves_named_conversation(recording, monkeypatch, text):
    names = []
    def ensure(**kwargs):
        names.append(kwargs)
        return kwargs["name"]
    response = Response(stream(("assistant.completed", {"content": "Answer."})))
    monkeypatch.setattr(sessions_api, "ensure_session", ensure)
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    assert sessions_api.complete(text, profile="music", session="alexa-hermes",
                                 session_title="Alexa via Hermes") == "Answer."
    assert names == [{"name": "alexa-hermes", "profile": "music", "title": "Alexa via Hermes"}]
    assert response.closed


@pytest.mark.parametrize("profile", [None, "sportscoach"])
def test_nonmusic_profile_does_not_rotate_on_audio_shaped_text(recording, monkeypatch, profile):
    names = []
    def ensure(**kwargs):
        names.append(kwargs["name"])
        return kwargs["name"]
    response = Response(stream(("assistant.completed", {"content": "Answer."})))
    monkeypatch.setattr(sessions_api, "ensure_session", ensure)
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    assert sessions_api.complete(prompt(recording), profile=profile,
                                 session="other-thread") == "Answer."
    assert names == ["other-thread"]


@pytest.mark.parametrize("failure", ["error", "run.failed"])
def test_recorded_stream_failure_closes_without_retry(recording, monkeypatch, failure):
    calls = []
    response = Response(stream((failure, {"message": "failure"})))
    monkeypatch.setattr(sessions_api, "ensure_session", lambda **k: k["name"])
    def post(*args, **kwargs):
        calls.append(kwargs)
        return response
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(sessions_api.SessionsAPIUnavailable, match="stream error"):
        sessions_api.complete(prompt(recording), profile="music")
    assert len(calls) == 1
    assert response.closed

"""Correlated playback telemetry; never contact HA or speakers."""

import asyncio
import copy

from plugins.voice_stack import ha_conversation as ha
from plugins.voice_stack import playback_verification as pv

COMMAND = "Play the album Boulevard by St Germain on Apple Music"
BASE = {
    "entity_id": "media_player.test",
    "state": "playing",
    "attributes": {
        "media_title": "BBC Radio 6 Music",
        "media_artist": "Music beyond the mainstream",
        "last_called_timestamp": 1000,
        "last_called_summary": COMMAND,
        "last_called_response": "I can't find that album.",
    },
}


def state(**attributes):
    result = copy.deepcopy(BASE)
    result["attributes"].update(attributes)
    return result


def test_old_playing_radio_and_stale_failure_are_not_new_evidence():
    assert pv.observation(COMMAND, BASE, BASE, 2000) is None


def test_new_rejection_catches_actual_failure_even_while_old_radio_is_playing():
    result = pv.observation(COMMAND, BASE, state(last_called_timestamp=3000), 2000)
    assert result["status"] == "failed"
    assert result["reason"] == "alexa_rejected_request"
    assert "can't find" in result["alexa_response"]


def test_rejection_for_another_command_is_not_assigned_to_this_request():
    result = pv.observation(
        COMMAND,
        BASE,
        state(last_called_timestamp=3000, last_called_summary="set an alarm"),
        2000,
    )
    assert result == {"status": "unverified", "reason": "another_command_observed"}


def test_success_requires_matching_fresh_media():
    current = state(
        media_title="Rose rouge",
        media_artist="St Germain",
        media_album_name="Boulevard",
        last_called_timestamp=3000,
        last_called_response="Playing Boulevard",
    )
    assert pv.observation(COMMAND, BASE, current, 2000)["status"] == "verified"
    assert pv.observation(COMMAND, current, current, 2000) is None
    assert pv.observation(COMMAND, None, current, 2000) is None


def test_song_requires_title_and_artist():
    command = "Play the song One by U2 on Apple Music"
    assert (
        pv.observation(
            command, BASE, state(media_title="One", media_artist="Metallica"), 2000
        )
        is None
    )
    assert (
        pv.observation(
            command,
            BASE,
            state(
                media_title="One",
                media_artist="U2",
                last_called_timestamp=3000,
                last_called_summary=command,
                last_called_response="Playing One",
            ),
            2000,
        )["status"]
        == "verified"
    )


def test_rejection_wins_over_matching_media():
    current = state(
        media_title="Rose rouge",
        media_artist="St Germain",
        media_album_name="Boulevard",
        last_called_timestamp=3000,
    )
    assert pv.observation(COMMAND, BASE, current, 2000)["status"] == "failed"


def test_playlist_activity_does_not_claim_playlist_identity():
    command = "Play the playlist Workout on Apple Music"
    current = state(
        media_title="One",
        media_artist="U2",
        last_called_timestamp=3000,
        last_called_summary=command,
        last_called_response="Playing Workout",
    )
    result = pv.observation(command, BASE, current, 2000)
    assert result["status"] == "unverified"
    assert result["reason"] == "playback_changed_selection_not_identified"


def test_poller_refreshes_then_detects_failure_without_replaying(monkeypatch):
    calls = []
    reads = iter([BASE, state(last_called_timestamp=3000)])

    async def read(*args, **kwargs):
        return next(reads)

    async def sleep(*args):
        pass

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def read(self):
            return b"[]"

    class Session:
        def post(self, url, **kwargs):
            calls.append(url)
            return Response()

    monkeypatch.setattr(pv, "read_player", read)
    monkeypatch.setattr(pv.asyncio, "sleep", sleep)
    result = asyncio.run(
        pv.verify(
            Session(), "http://ha.test", {}, "media_player.test", COMMAND, BASE, 2000
        )
    )
    assert result["status"] == "failed" and result["audibility"] == "unverified"
    assert calls == [
        "http://ha.test/api/services/alexa_media/update_last_called",
        "http://ha.test/api/services/homeassistant/update_entity",
    ]


def test_timeout_is_unverified_not_success_or_failure(monkeypatch):
    clock = iter(range(100))
    from types import SimpleNamespace

    monkeypatch.setattr(pv, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    async def read(*args, **kwargs):
        return BASE

    async def sleep(*args):
        pass

    monkeypatch.setattr(pv, "read_player", read)
    monkeypatch.setattr(pv.asyncio, "sleep", sleep)

    class Session:
        def post(self, *args, **kwargs):
            raise OSError("refresh unavailable")

    result = asyncio.run(
        pv.verify(
            Session(),
            "http://ha.test",
            {},
            "media_player.test",
            COMMAND,
            BASE,
            2000,
            timeout=8,
        )
    )
    assert result["status"] == "unverified"
    assert result["reason"] == "no_fresh_playback_evidence"


def test_accepted_dispatch_always_checks_result(monkeypatch):
    calls = []

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def json(self):
            return []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, url, **kwargs):
            calls.append(url)
            return Response()

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    monkeypatch.setattr(ha, "_get_config", lambda: ("http://ha.test", "fixture"))

    async def baseline(*args, **kwargs):
        return BASE

    async def verify(*args, **kwargs):
        calls.append("verification")
        return {"status": "failed", "reason": "alexa_rejected_request"}

    monkeypatch.setattr(pv, "read_player", baseline)
    monkeypatch.setattr(pv, "verify", verify)
    result = ha.play_alexa_media(COMMAND, "media_player.test")
    assert result["ok"] is True  # Transport acceptance is retained separately.
    assert result["playback_verification"]["status"] == "failed"
    assert calls == [
        "http://ha.test/api/services/media_player/play_media",
        "verification",
    ]


def test_actual_genre_case_verifies_start_without_a_fixed_playlist_identity():
    command = "Play chill house jazz music on Apple Music"
    current = state(
        media_title="Chill Jazz Beats Playlist",
        media_artist="Chill Jazz",
        media_album_name="Apple Music",
        media_content_type="music",
        last_called_timestamp=3000,
        last_called_summary=command,
        last_called_response=None,
    )
    result = pv.observation(command, BASE, current, 2000)
    assert result["status"] == "verified"
    assert result["verification_scope"] == "playback_started"
    assert result["playback_state"] == "playing"
    assert result["media_title"] == "Chill Jazz Beats Playlist"


def test_genre_request_still_rejects_old_radio_and_unrelated_commands():
    command = "Play chill house jazz music on Apple Music"
    assert pv.observation(command, BASE, BASE, 2000) is None
    current = state(
        media_title="Chill Jazz Beats Playlist",
        media_artist="Chill Jazz",
        media_content_type="music",
        last_called_timestamp=3000,
        last_called_summary="Play the radio",
        last_called_response=None,
    )
    assert pv.observation(command, BASE, current, 2000)["status"] == "unverified"


def test_matching_metadata_without_new_command_does_not_verify_selection():
    current = state(
        media_title="Rose rouge",
        media_artist="St Germain",
        media_album_name="Boulevard",
    )
    assert pv.observation(COMMAND, BASE, current, 2000) is None



def test_poller_does_not_stop_when_previous_queue_advances(monkeypatch):
    command = "Play the song Agitations tropicales by L'Impératrice on Apple Music"
    old_queue = state(media_title="Walk Your Talk", media_artist="Chill Jazz",
                      last_called_timestamp=3000, last_called_summary=command,
                      last_called_response=None)
    requested = state(media_title="Agitations tropicales", media_artist="L'Impératrice",
                      last_called_timestamp=3000, last_called_summary=command,
                      last_called_response=None)
    reads = iter([old_queue, requested])
    count = []
    async def read(*a, **kw):
        count.append(1)
        return next(reads)
    async def sleep(*a):
        pass
    class Session:
        def post(self, *a, **kw):
            raise OSError("refresh unavailable")
    monkeypatch.setattr(pv, "read_player", read)
    monkeypatch.setattr(pv.asyncio, "sleep", sleep)
    result = asyncio.run(pv.verify(Session(), "http://test", {}, "media_player.test",
                                  command, BASE, 2000))
    assert len(count) == 2
    assert result["status"] == "verified"
    assert result["media_title"] == "Agitations tropicales"


def test_poller_detects_delayed_rejection_after_old_queue_change(monkeypatch):
    current = state(media_title="Another old track", last_called_timestamp=3000,
                    last_called_response=None)
    rejection = state(last_called_timestamp=3000, last_called_response="I can't find that album")
    reads = iter([current, rejection])
    async def read(*a, **kw):
        return next(reads)
    async def sleep(*a):
        pass
    class Session:
        def post(self, *a, **kw):
            raise OSError("refresh unavailable")
    monkeypatch.setattr(pv, "read_player", read)
    monkeypatch.setattr(pv.asyncio, "sleep", sleep)
    result = asyncio.run(pv.verify(Session(), "http://test", {}, "media_player.test", COMMAND, BASE, 2000))
    assert result["status"] == "failed"

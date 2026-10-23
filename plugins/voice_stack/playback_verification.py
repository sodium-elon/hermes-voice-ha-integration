"""Observe Alexa's response and player state after a single accepted dispatch.

This module never sends playback commands. Device telemetry is not proof of
physical audibility, and an old playing state is not evidence for a new request.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata

logger = logging.getLogger(__name__)

VERIFY_SECONDS = 15.0
POLL_SECONDS = 2.0
FAILURE = re.compile(
    r"(?:can(?:not|'t)|could(?: not|n't)) (?:find|play|start|do that)"
    r"|unable to (?:find|play)|having trouble|not available|not supported"
    r"|something went wrong|(?:link|renew) your|subscription (?:is )?required"
    r"|don't know (?:that|how)",
    re.IGNORECASE,
)


def normalized(value):
    value = (
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode()
    )
    return " ".join(
        re.sub(r"[^a-z0-9]+", " ", value.casefold().replace("&", " and ")).split()
    )


def _attributes(state):
    return (
        state.get("attributes", {})
        if isinstance(state, dict) and isinstance(state.get("attributes"), dict)
        else {}
    )


def _timestamp(state):
    try:
        return float(_attributes(state).get("last_called_timestamp", 0))
    except (TypeError, ValueError):
        return 0


def _media(state):
    attrs = _attributes(state)
    return tuple(
        attrs.get(k)
        for k in ("media_title", "media_artist", "media_album_name", "media_content_id")
    )


def _matches_selection(command, state):
    attrs = _attributes(state)
    request = re.fullmatch(
        r"Play the (song|album) (.+) by (.+) on Apple Music", command
    )
    if request:
        kind, title, artist = request.groups()
        actual_title = attrs.get(
            "media_title" if kind == "song" else "media_album_name"
        )
        return normalized(title) == normalized(actual_title) and normalized(
            artist
        ) == normalized(attrs.get("media_artist"))
    request = re.fullmatch(
        r"(?:Play songs|Shuffle music) by (.+) on Apple Music", command
    )
    if request:
        return normalized(request[1]) == normalized(attrs.get("media_artist"))
    # Playlist/genre identity is not exposed consistently by Alexa Media Player.
    return False


def observation(command, baseline, current, sent_at_ms):
    """Return decisive evidence, or None to continue observing."""
    if not isinstance(current, dict):
        return None
    attrs = _attributes(current)
    fresh_response = (
        _timestamp(current) > _timestamp(baseline) and _timestamp(current) >= sent_at_ms
    )
    command_matches = normalized(attrs.get("last_called_summary")) == normalized(
        command
    )
    if (
        fresh_response
        and command_matches
        and FAILURE.search(str(attrs.get("last_called_response", "")))
    ):
        return {
            "status": "failed",
            "reason": "alexa_rejected_request",
            "alexa_response": str(attrs["last_called_response"])[:500],
        }
    if fresh_response and attrs.get("last_called_summary") and not command_matches:
        return {"status": "unverified", "reason": "another_command_observed"}
    # No baseline means a change cannot be attributed to this dispatch.
    changed = baseline is not None and (
        _media(current) != _media(baseline)
        or (baseline.get("state") != "playing" and current.get("state") == "playing")
    )
    if (
        current.get("state") == "playing"
        and changed
        and fresh_response
        and command_matches
        and _matches_selection(command, current)
    ):
        return {
            "status": "verified",
            "reason": "requested_media_observed",
            "verification_scope": "requested_selection",
            "playback_state": "playing",
            "media_title": attrs.get("media_title"),
            "media_artist": attrs.get("media_artist"),
            "media_album_name": attrs.get("media_album_name"),
        }
    # A genre request has no fixed song/playlist identity. A correlated command
    # and changed music metadata can establish playback without claiming genre
    # membership or physical audibility.
    genre_request = not command.startswith("Play the ") and re.fullmatch(
        r"Play .+ music on Apple Music", command
    )
    if (
        genre_request
        and current.get("state") == "playing"
        and changed
        and fresh_response
        and command_matches
        and attrs.get("media_content_type") == "music"
        and attrs.get("media_title")
    ):
        return {
            "status": "verified",
            "reason": "fresh_music_after_genre_request",
            "verification_scope": "playback_started",
            "playback_state": "playing",
            "media_title": attrs.get("media_title"),
            "media_artist": attrs.get("media_artist"),
        }
    # A named selection still needs identity evidence. Distinguish observed
    # playback from knowing that the requested song/album/playlist was selected.
    if (
        current.get("state") == "playing"
        and changed
        and fresh_response
        and command_matches
    ):
        return {
            "status": "unverified",
            "reason": "playback_changed_selection_not_identified",
            "verification_scope": "selection_identity",
            "playback_state": "playing",
            "media_title": attrs.get("media_title"),
            "media_artist": attrs.get("media_artist"),
        }
    return None


async def read_player(session, url, headers, target, timeout=2.0):
    import aiohttp

    async with session.get(
        f"{url}/api/states/{target}",
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as response:
        response.raise_for_status()
        state = await response.json()
        if not isinstance(state, dict) or state.get("entity_id") != target:
            raise ValueError("Unexpected player state")
        return state


async def verify(
    session,
    url,
    headers,
    target,
    command,
    baseline,
    sent_at_ms,
    *,
    timeout=VERIFY_SECONDS,
):
    import aiohttp

    deadline = time.monotonic() + timeout
    reads = 0
    latest_inconclusive = None
    next_history_refresh = time.monotonic()
    while time.monotonic() < deadline:
        if time.monotonic() >= next_history_refresh:
            try:
                # Player refresh alone does not fetch Alexa response history.
                # This service refreshes account history without speaking/playing.
                async with session.post(
                    f"{url}/api/services/alexa_media/update_last_called",
                    headers=headers,
                    json={},
                    timeout=aiohttp.ClientTimeout(
                        total=min(2.0, max(0.05, deadline - time.monotonic()))
                    ),
                ) as response:
                    response.raise_for_status()
                    await response.read()
            except (
                aiohttp.ClientError,
                TimeoutError,
                OSError,
                ValueError,
                TypeError,
            ) as exc:
                logger.debug(
                    "Alexa history refresh unavailable: %s", type(exc).__name__
                )
            next_history_refresh = time.monotonic() + 7.5
        try:
            current = await read_player(
                session,
                url,
                headers,
                target,
                timeout=min(2.0, max(0.05, deadline - time.monotonic())),
            )
            reads += 1
            result = observation(command, baseline, current, sent_at_ms)
            if result:
                if result["status"] in ("verified", "failed") or result.get("reason") == "another_command_observed":
                    return {**result, "audibility": "unverified"}
                # An old queue advancing is inconclusive. Let the requested
                # selection or a delayed rejection arrive within the budget.
                latest_inconclusive = result
        except (
            aiohttp.ClientError,
            TimeoutError,
            OSError,
            ValueError,
            TypeError,
        ) as exc:
            logger.debug("Playback observation unavailable: %s", type(exc).__name__)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            # Refresh telemetry only; never issue a second playback command.
            async with session.post(
                f"{url}/api/services/homeassistant/update_entity",
                headers=headers,
                json={"entity_id": target},
                timeout=aiohttp.ClientTimeout(total=min(2.0, remaining)),
            ) as response:
                response.raise_for_status()
                await response.read()
        except (
            aiohttp.ClientError,
            TimeoutError,
            OSError,
            ValueError,
            TypeError,
        ) as exc:
            logger.debug("Playback refresh unavailable: %s", type(exc).__name__)
        await asyncio.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    if latest_inconclusive:
        return {**latest_inconclusive, "audibility": "unverified"}
    return {
        "status": "unverified",
        "reason": "no_fresh_playback_evidence" if reads else "telemetry_unavailable",
        "audibility": "unverified",
    }

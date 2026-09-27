"""Home Assistant Conversation API client for the isolated voice-stack plugin."""

from __future__ import annotations

import asyncio
import argparse
import concurrent.futures
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://homeassistant.local:8123"
_DEFAULT_AGENT = "conversation.home_assistant"
_CONVERSATION_TIMEOUT = 15.0
_CHANNEL_VERIFY_TIMEOUT = 10.0
_CHANNEL_MEDIA_PLAYER = "media_player.firedroid"
_MEDIA_SERVICE_TIMEOUT = 10.0
_THREAD_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="voice-ha-conversation-"
)


def _get_config() -> tuple[str, str]:
    url = os.getenv("HASS_URL", "")
    token = os.getenv("HASS_TOKEN", "")
    if not token:
        # Profile gateways do not inherit the default profile's .env. Reuse the
        # machine-local HA credentials without copying secrets between profiles.
        try:
            from dotenv import dotenv_values

            shared = dotenv_values(Path.home() / ".hermes" / ".env")
            url = url or str(shared.get("HASS_URL") or "")
            token = str(shared.get("HASS_TOKEN") or "")
        except Exception:
            pass
    return ((url or _DEFAULT_URL).rstrip("/"), token)


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return _THREAD_POOL.submit(asyncio.run, coro).result(timeout=30)
    return asyncio.run(coro)


async def _async_process_conversation(
    text: str,
    language: str,
    conversation_id: Optional[str],
    agent_id: str,
) -> Dict[str, Any]:
    import aiohttp

    url, token = _get_config()
    if not token:
        raise RuntimeError("HASS_TOKEN is not configured")

    payload: Dict[str, Any] = {
        "text": text,
        "language": language,
        "agent_id": agent_id,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{url}/api/conversation/process",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=_CONVERSATION_TIMEOUT),
        ) as response:
            response.raise_for_status()
            return await response.json()


def process_conversation(
    text: str,
    *,
    language: str = "en",
    conversation_id: Optional[str] = None,
    agent_id: str = _DEFAULT_AGENT,
) -> Dict[str, Any]:
    """Send text directly to HA's built-in agent without Hermes recursion."""
    try:
        return _run_async(
            _async_process_conversation(text, language, conversation_id, agent_id)
        )
    except Exception as exc:
        logger.error("HA conversation processing failed: %s", exc)
        return {"error": f"Home Assistant conversation failed: {exc}"}


async def _async_play_alexa_media(command: str, target: str) -> Dict[str, Any]:
    """Send an Alexa custom media command through Home Assistant."""
    import aiohttp

    url, token = _get_config()
    if not token:
        raise RuntimeError("HASS_TOKEN is not configured")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "entity_id": target,
        "media_content_type": "custom",
        "media_content_id": command,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{url}/api/services/media_player/play_media",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=_MEDIA_SERVICE_TIMEOUT),
        ) as response:
            response.raise_for_status()
            await response.read()
    return {"ok": True, "target": target, "command": command}


def play_alexa_media(command: str, target: str) -> Dict[str, Any]:
    """Run an Alexa custom media command from the background voice thread."""
    try:
        result = _run_async(_async_play_alexa_media(command, target))
        return {**result, "acceptance_kind": "ha_accepted_unverified" if result.get("ok") is True else "ha_failed"}
    except Exception as exc:
        logger.error("Alexa media playback failed: %s", exc)
        return {"ok": False, "acceptance_kind": "ha_failed", "reason": "ha_error", "error": str(exc)}


_NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}


def _normalize_channel_name(value: str) -> str:
    """Normalize STT and HA channel names without a static channel mapping."""
    words = re.findall(r"[a-z0-9]+", value.lower())
    normalized = [str(_NUMBER_WORDS.get(word) or word) for word in words]
    return "".join(normalized)


def select_live_channel(states: list[dict], target: str) -> Optional[dict]:
    """Return one available live channel script matching the spoken target."""
    wanted = _normalize_channel_name(target)
    matches = []
    for state in states:
        entity_id = str(state.get("entity_id") or "")
        if not entity_id.startswith("script.channel_"):
            continue
        if str(state.get("state") or "").lower() == "unavailable":
            continue
        attributes = state.get("attributes") or {}
        friendly_name = str(attributes.get("friendly_name") or "")
        entity_name = entity_id.removeprefix("script.channel_")
        if wanted in {
            _normalize_channel_name(friendly_name),
            _normalize_channel_name(entity_name),
        }:
            matches.append(state)
    return matches[0] if len(matches) == 1 else None


async def _async_run_live_channel(target: str) -> Dict[str, Any]:
    """Resolve and run a channel from HA's current states, then verify playback."""
    import aiohttp

    url, token = _get_config()
    if not token:
        raise RuntimeError("HASS_TOKEN is not configured")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=_CHANNEL_VERIFY_TIMEOUT + 15.0)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{url}/api/states", headers=headers) as response:
            response.raise_for_status()
            states = await response.json()

        selected = select_live_channel(states, target)
        if selected is None:
            return {"ok": False, "reason": "no_unique_match"}

        entity_id = selected["entity_id"]
        name = str((selected.get("attributes") or {}).get("friendly_name") or entity_id)
        old_trigger = (selected.get("attributes") or {}).get("last_triggered")

        old_media_title = None
        async with session.get(
            f"{url}/api/states/{_CHANNEL_MEDIA_PLAYER}", headers=headers
        ) as response:
            if response.status == 200:
                media = await response.json()
                old_media_title = (media.get("attributes") or {}).get("media_title")

        async with session.post(
            f"{url}/api/services/script/turn_on",
            headers=headers,
            json={"entity_id": entity_id},
        ) as response:
            response.raise_for_status()

        deadline = time.monotonic() + _CHANNEL_VERIFY_TIMEOUT
        trigger_seen = False
        latest_title = old_media_title
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            async with session.get(f"{url}/api/states/{entity_id}", headers=headers) as response:
                response.raise_for_status()
                script_state = await response.json()
                trigger = (script_state.get("attributes") or {}).get("last_triggered")
                trigger_seen = bool(trigger and trigger != old_trigger)
            async with session.get(
                f"{url}/api/states/{_CHANNEL_MEDIA_PLAYER}", headers=headers
            ) as response:
                if response.status == 200:
                    media = await response.json()
                    latest_title = (media.get("attributes") or {}).get("media_title")
            if trigger_seen and latest_title and latest_title != old_media_title:
                return {
                    "ok": True,
                    "entity_id": entity_id,
                    "name": name,
                    "media_title": latest_title,
                }

        return {
            "ok": False,
            "reason": "verification_failed",
            "entity_id": entity_id,
            "name": name,
            "trigger_seen": trigger_seen,
            "media_title": latest_title,
        }


def run_live_channel(target: str) -> Dict[str, Any]:
    """Synchronous live-channel resolver for the background voice thread."""
    try:
        return _run_async(_async_run_live_channel(target))
    except Exception as exc:
        logger.error("Live HA channel execution failed: %s", exc)
        return {"ok": False, "reason": "ha_error", "error": str(exc)}


def _main() -> int:
    parser = argparse.ArgumentParser(description="Home Assistant voice-stack client")
    parser.add_argument("--script", choices=("play_alexa_media",), required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--target", default=os.getenv("HERMES_ALEXA_MEDIA_PLAYER", ""))
    args = parser.parse_args()
    if not args.target:
        parser.error("--target or HERMES_ALEXA_MEDIA_PLAYER is required")
    result = play_alexa_media(args.text, args.target)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())

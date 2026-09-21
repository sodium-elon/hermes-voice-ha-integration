"""Hermes Sessions API client for the voice stack.

Replaces the previous approach of shelling out to ``hermes chat`` for every
unmatched voice query. That subprocess paid a full CLI cold start — plugin
discovery across ~55 plugins — before the model was even called, roughly five
to six seconds per turn, and the answer could only be recovered by scraping
stdout.

Talking to the gateway's API-server platform instead keeps the agent warm in a
process that is already running, and the SSE stream reports tool calls *as they
happen* rather than leaving them to be reconstructed from ``agent.log``.

The SSE parsing here follows the approach in ``eadmin2/jarvis_ai`` (MIT), which
drives the same Hermes surface; two of its hard-won details are kept verbatim
in spirit: forcing UTF-8 on the response (SSE carries no charset, and requests
would otherwise assume latin-1 and produce mojibake) and always closing the
response so long-lived streams cannot leak file descriptors.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Iterator, Optional

from . import events

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8642"
DEFAULT_PROFILE_BASE_URLS = {
    "sportscoach": "http://127.0.0.1:8643",
    "music": "http://127.0.0.1:8644",
}
DEFAULT_SESSION_ID = "voice-stack"
CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 180.0


class SessionsAPIUnavailable(RuntimeError):
    """Raised when the API server is not reachable or not configured."""


def base_url(profile: Optional[str] = None) -> str:
    if profile:
        env_name = f"HERMES_{profile.upper().replace('-', '_')}_API_BASE_URL"
        configured = os.getenv(env_name)
        default = DEFAULT_PROFILE_BASE_URLS.get(profile)
        if configured or default:
            return (configured or default or "").rstrip("/")
    return (os.getenv("HERMES_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def session_id() -> str:
    return os.getenv("HERMES_VOICE_SESSION_ID") or DEFAULT_SESSION_ID


def _api_key() -> str:
    key = (os.getenv("API_SERVER_KEY") or os.getenv("HERMES_API_KEY") or "").strip()
    if not key:
        raise SessionsAPIUnavailable(
            "API_SERVER_KEY is not set — the Hermes API server rejects unauthenticated calls"
        )
    return key


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}


def ensure_session(
    name: Optional[str] = None,
    *,
    profile: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """Create the voice session if it does not exist yet; return its id.

    A fixed id is used rather than a persisted mapping file: re-creating it is
    a single idempotent call, and a 409 simply means it already exists.
    """
    import requests

    sid = name or session_id()
    try:
        response = requests.post(
            f"{base_url(profile)}/api/sessions",
            headers=_headers(),
            json={"id": sid, "title": title or "Voice (Hey Jarvis)"},
            timeout=CONNECT_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SessionsAPIUnavailable(f"Hermes API server unreachable: {exc}") from exc

    if response.status_code == 409:
        return sid  # already created by an earlier turn
    if response.status_code >= 400:
        raise SessionsAPIUnavailable(
            f"session create failed: HTTP {response.status_code} {response.text[:200]}"
        )
    logger.info("Created Hermes voice session %s", sid)
    return sid


def _iter_sse(response: Any) -> Iterator[tuple[str, dict]]:
    """Yield ``(event_name, payload)`` pairs from an SSE response."""
    event_name = ""
    for raw in response.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        if raw.startswith("event: "):
            event_name = raw[7:].strip()
            continue
        if not raw.startswith("data: "):
            continue
        try:
            payload = json.loads(raw[6:].strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        yield (event_name or str(payload.get("event") or ""), payload)


def complete(
    text: str,
    *,
    profile: Optional[str] = None,
    system_prompt: Optional[str] = None,
    timeout: Optional[float] = None,
    on_tool: Optional[Callable[[str, str, dict], None]] = None,
    session: Optional[str] = None,
    session_title: Optional[str] = None,
) -> str:
    """Run one voice turn through the Sessions API and return the spoken text.

    ``on_tool`` is invoked as ``(phase, tool_name, payload)`` for every tool
    lifecycle event, so the caller can surface tool activity live.
    ``session`` overrides the default session id, letting separate voice
    surfaces keep their own conversation threads. ``session_title`` controls
    the title used when creating that session.
    """
    import requests

    sid = ensure_session(name=session, profile=profile, title=session_title)
    read_timeout = timeout if timeout is not None else float(
        os.getenv("HERMES_VOICE_AGENT_TIMEOUT", str(DEFAULT_READ_TIMEOUT))
    )

    body: dict[str, Any] = {"input": text}
    if system_prompt:
        body["system_message"] = system_prompt

    try:
        response = requests.post(
            f"{base_url(profile)}/api/sessions/{sid}/chat/stream",
            headers={**_headers(), "Accept": "text/event-stream"},
            json=body,
            stream=True,
            timeout=(CONNECT_TIMEOUT, read_timeout),
        )
    except requests.RequestException as exc:
        raise SessionsAPIUnavailable(f"Hermes API server unreachable: {exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:300]
        response.close()
        raise SessionsAPIUnavailable(f"chat/stream HTTP {response.status_code}: {detail}")

    # SSE responses carry no charset, and requests would fall back to latin-1
    # and mangle anything non-ASCII on its way to TTS.
    response.encoding = "utf-8"

    deltas: list[str] = []
    final_text = ""
    try:
        for name, payload in _iter_sse(response):
            if name == "run.started":
                events.emit("agent_run", runner="sessions api", run_id=payload.get("run_id"), session=sid)

            elif name == "assistant.delta":
                delta = payload.get("delta") or ""
                if delta:
                    deltas.append(delta)

            elif name in {"tool.started", "tool.completed", "tool.failed"}:
                tool_name = str(payload.get("tool_name") or "tool")
                if tool_name.startswith("_"):
                    continue  # internal pseudo-tools such as _thinking
                phase = name.split(".", 1)[1]
                events.emit(
                    "tool",
                    phase=phase,
                    tool=tool_name,
                    preview=events.truncate(str(payload.get("preview") or ""), 200) or None,
                    seconds=payload.get("duration"),
                )
                if on_tool is not None:
                    try:
                        on_tool(phase, tool_name, payload)
                    except Exception:  # pragma: no cover - observer must not break the turn
                        logger.debug("on_tool observer failed", exc_info=True)

            elif name == "assistant.completed":
                final_text = str(payload.get("content") or "").strip()

            elif name in {"run.failed", "error"}:
                raise SessionsAPIUnavailable(f"stream error: {json.dumps(payload)[:300]}")

            elif name == "run.completed":
                usage = payload.get("usage") or {}
                if usage:
                    events.emit(
                        "usage",
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                    )
    finally:
        # A leaked stream here would hold a socket open for the lifetime of the
        # gateway; the voice loop runs indefinitely.
        response.close()

    return final_text or "".join(deltas).strip()


def available() -> bool:
    """Cheap readiness probe used by voice_status."""
    try:
        _api_key()
    except SessionsAPIUnavailable:
        return False
    try:
        import requests

        response = requests.get(f"{base_url()}/health", timeout=3)
        return response.status_code < 500
    except Exception:
        return False

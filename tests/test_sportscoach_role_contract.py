"""Source-owned Sportscoach role boundaries; no live services or profile writes."""
import json
from pathlib import Path

import pytest

from plugins.voice_stack import VOICE_AGENT_SYSTEM_PROMPT, sessions_api

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/profiles/sportscoach/alexa-voice-replies/SKILL.md"


@pytest.mark.parametrize("clause", [
    "Ordinary coaching and voice requests do not authorize coding, configuration changes, ingestion repair, or scheduled tasks.",
    "Use Sportscoach's own tools and skills for legitimate coaching; do not turn missing data or a lost spoken answer into unsolicited maintenance.",
    "Load the main AlphaRunner `running-coaching` skill for coaching doctrine; do not duplicate its domain procedures here or in relay prompts.",
    "A protected skill correction refusal is a boundary to respect, not a failed learning task to bypass.",
    "Preserve the proposed learning correction and its evidence in the conversation; state that it is not installed.",
    "Route the proposal through the owning AlphaRunner source and a reviewed PR; never bypass protection with shell writes, another profile, or an alternate tool.",
    "Use this branch only for explicitly requested engineering diagnostics or listening repair, not merely because John reports a lost spoken answer.",
])
def test_sportscoach_role_and_learning_contract(clause):
    assert clause in SKILL.read_text()


def test_cross_repository_policy_ownership_is_documented():
    docs = (ROOT / "docs/runtime-source-sync.md").read_text()
    assert "deploy/sportscoach-profile-policy.json" in docs
    assert "does not grant additional tools" in docs


@pytest.mark.parametrize("profile", ["sportscoach", None])
def test_relay_keeps_target_profile_capabilities_without_toolset_overrides(
    tmp_path, monkeypatch, profile
):
    import requests
    import subprocess

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("API_SERVER_KEY", "fixture-key")
    monkeypatch.delenv("HERMES_API_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_SPORTSCOACH_API_BASE_URL", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail(
        "Ordinary coaching must not launch the dated-shoe preflight"))
    calls = []

    class Response:
        status_code = 200
        encoding = "utf-8"

        def iter_lines(self, **kwargs):
            yield "event: assistant.completed"
            yield 'data: {"content":"Take an easy recovery day."}'

        def close(self):
            pass

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return Response()

    monkeypatch.setattr(requests, "post", post)
    text = "What recovery session should I do today?"
    assert sessions_api.complete(text, profile=profile,
        system_prompt=VOICE_AGENT_SYSTEM_PROMPT, session="role-contract"
    ) == "Take an easy recovery day."
    port = 8643 if profile else 8642
    assert calls[0] == (f"http://127.0.0.1:{port}/api/sessions", {
        "id": "role-contract", "title": "Voice (Hey Jarvis)"})
    url, body = calls[1]
    assert url == f"http://127.0.0.1:{port}/api/sessions/role-contract/chat/stream"
    assert set(body) == {"input", "system_message"}
    if profile:
        original, envelope = body["input"].split("\n\n[Voice transport receipt]\n")
        assert original == text
        receipt = json.loads(Path(json.loads(envelope)["receipt_path"]).read_text())
        assert receipt["profile"] == profile
        assert receipt["text"] == text
        assert receipt["session"] == "role-contract"
        assert body["system_message"] == (
            VOICE_AGENT_SYSTEM_PROMPT + "\n\n" + sessions_api.VOICE_PROVENANCE_INSTRUCTIONS)
    else:
        assert body == {"input": text, "system_message": VOICE_AGENT_SYSTEM_PROMPT}

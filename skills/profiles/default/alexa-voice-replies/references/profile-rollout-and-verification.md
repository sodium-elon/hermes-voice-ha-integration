# Alexa Voice Reply Rollout and Failure Diagnosis

Use this reference when Alexa TTS works for one Hermes profile/channel but another profile loops through terminal code, reports success without sound, or ignores the workflow skill.

## Capability layers

Verify each layer separately:

1. **Skill visibility** — `alexa-voice-replies` is installed and enabled in the target profile.
2. **Platform tool exposure** — `homeassistant` is enabled for the actual inbound platform, not merely CLI. Check Telegram and Discord independently.
3. **Profile credentials** — the target profile has its own `HASS_URL` and `HASS_TOKEN`; do not teach one profile to read secrets from another profile’s root.
4. **Gateway lifecycle** — environment and platform-toolset changes require an explicitly authorized restart of only the affected gateway.
5. **Session lifecycle** — an existing conversation can retain an older skill/tool catalog after installation. Use `/reset` when a fresh gateway still ignores a newly added skill.

A skill on disk does not grant tool access. Conversely, an enabled toolset in CLI does not prove that Discord or Telegram exposes it.

## Known-good Alexa TTS contract

```text
ha_call_service(
  domain="notify",
  service="alexa_media",
  data='{"message":"<exact text>","target":["media_player.john_s_echo_5th_right"],"data":{"type":"tts"}}'
)
```

John’s default target is the right Echo only. Use the left Echo or both speakers only when explicitly requested.

## Single-speaker isolation and Amazon grouping

The normal `notify.alexa_media` contract may be accepted with only the right entity in `target`, yet John may physically hear both Echoes. Do not infer the actual acoustic route from the HA request body or an `idle` player state.

Run one device-specific probe:

```text
ha_call_service(
  domain="notify",
  service="alexa_media_john_s_echo_5th_right",
  data='{"message":"Right speaker test","data":{"type":"tts"}}'
)
```

Interpretation requires John’s physical observation:

- **Right only:** use the device-specific service when strict single-speaker routing matters.
- **Both:** Amazon-side stereo pairing or speaker grouping is the leading hypothesis. The HA call was unicast, but Amazon expanded playback. Inspect Amazon device topology; never silently dissolve the pair.
- **Silence:** the service was accepted but the audible outcome failed. Re-check availability, mute, volume, Alexa Media authentication, and integration logs.

Do not confuse this valid device-specific service with the broken pattern `service="send_message"` plus `entity_id="notify.alexa_media_<device>"`. They are different API contracts.

## Misleading success pattern

This sequence is a failure even if the second call returns `success: true`:

1. Call `notify.send_message` with `entity_id="notify.alexa_media"` and receive HTTP 400.
2. Retry `notify.send_message` with `entity_id="notify.alexa_media_<device>"`.
3. Report that Alexa spoke.

Why it fails operationally:

- `notify.send_message` is not the verified Alexa Media TTS contract.
- A successful generic notification only proves Home Assistant accepted that service call.
- Missing `data.type="tts"` means spoken output was never requested through the known-good path.
- An Alexa player remaining `idle` neither proves nor disproves audio playback.

## Response integrity

- Preserve the exact requested words or the exact prior answer John asked to repeat.
- Do not add persona prefixes such as “Elon says,” “Coach says,” or “Ria says.”
- Report the exact target and service acceptance.
- Never state that sound was heard unless device telemetry proves it.
- If John says he heard nothing, treat that as a failed user outcome even when Home Assistant accepted the command; inspect service shape, target availability, mute state, and volume before retrying.

## Efficient diagnostic sequence

1. Identify the actual originating profile/channel from the session database or gateway route before editing anything; a pasted reply or persona-like wording is not reliable attribution.
2. Read that target-profile session or logs and capture the exact service arguments and result.
3. Compare them with the known-good contract above.
4. Fix profile/platform capability rather than adding REST or terminal workarounds.
5. Patch the shared umbrella skill and every explicitly requested profile-local copy that is in scope.
6. Restart only authorized gateways and verify replacement PIDs plus active state.
7. Start a fresh target session when skill discovery was stale.
8. Retry speech only when John requests it; do not unexpectedly broadcast during configuration repair.
9. For routing tests, require John’s physical observation (`right only`, `both`, or `silence`) before declaring the routing behavior verified.

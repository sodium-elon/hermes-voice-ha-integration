---
name: alexa-voice-replies
description: "Use immediately when John says answer through Alexa, reply through Alexa, say this on Alexa, speak through Alexa, or asks for voice on Echo speakers. Also load when diagnosing the Alexa skill path (Echo → Lambda → HA → Hermes conversation.hermes) — missing Alexa turns, zombie HA WebSocket, or distinguishing Alexa skill from hey_jarvis. Use Home Assistant notify.alexa_media TTS directly; never use notify.send_message and never add an intro or alter the requested words."
version: 1.5.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [voice, alexa, echo, home-assistant, tts]
    related_skills: [home-assistant]
---

# Alexa Voice Replies

## Trigger

Load and execute this skill immediately when John asks to answer, reply, say, or speak through Alexa/Echo. Do not substitute Telegram audio unless he explicitly requests a Telegram voice message.

When rolling this capability out to another Hermes profile/channel, or when Home Assistant accepts a command but John hears nothing, follow `references/profile-rollout-and-verification.md`. It separates skill visibility, platform tool exposure, profile credentials, gateway reloads, stale sessions, and misleading notification success.

When a single logical target is heard from multiple physical Echos, follow `references/alexa-routing-and-verification.md`. It covers device-specific isolation probes, Amazon stereo/group routing, and the requirement to use John's physical observation rather than API-success theater.

When diagnosing `hey_jarvis` remotely, follow `references/hey-jarvis-remote-diagnostics.md`. Confirm whether anyone is physically near Tungsten before recording; otherwise silence proves only absence. The reference separates Alexa input, Echo TTS, and Tungsten wake detection, and defines a synthetic Echo-to-room path test plus score/bridge/event verification.

When extending Jarvis music playback beyond hardcoded phrases, follow `references/dynamic-music-resolution.md`. It covers the TypeSafe/Jev decision layer (classify + select, not raw llm), the exact outage contract, collaboration phrasing, deterministic-recency vs. judgment, `_strip_verbs` anchoring, STT-mishearing diagnostics, the dry-run toggle, direct Alexa pass-through vs. descriptive clue resolution, validating the generated command, reconciling canonical and installed plugin sources, and requiring event plus physical playback proof.

## Exact Workflow

1. Call `ha_list_entities(domain="media_player")` once and keep available physical Echo targets.
2. By default, use the right Echo's device-specific Alexa Media service:
   - media player: `media_player.john_s_echo_5th_right`
   - notify service: `notify.alexa_media_john_s_echo_5th_right`
   Use the left Echo or both speakers only when John explicitly asks.
3. Send the exact requested text with this default service shape:

```text
ha_call_service(
  domain="notify",
  service="alexa_media_john_s_echo_5th_right",
  data='{"message":"<exact requested text>","data":{"type":"tts"}}'
)
```

   For an explicitly requested left-only reply, use `notify.alexa_media_echo_dot_5th_left`. For an explicitly requested multi-speaker reply, use `notify.alexa_media` with the requested media players in its `target` array.
4. Only report that Home Assistant accepted the TTS command when the result contains `success: true`.
5. Re-read the selected target to confirm it remains available, unmuted, and at a usable volume. An `idle` state does not prove or disprove audible playback.
6. When John is testing routing, ask what he physically heard: right only, both, or silence. His observation is the completion signal; API acceptance alone is not.
7. If a right-only request is heard from both speakers, run one controlled device-specific probe before changing configuration:

```text
ha_call_service(
  domain="notify",
  service="alexa_media_john_s_echo_5th_right",
  data='{"message":"Right speaker test","data":{"type":"tts"}}'
)
```

If that probe is also heard from both, treat Amazon speaker grouping/stereo pairing as the leading hypothesis. Do not claim Home Assistant targeted both, and do not break the pair without John’s explicit approval.

## Non-Negotiable Pitfalls

- **Never use `notify.send_message`** for Alexa voice. It can return success without invoking Alexa TTS. The device-specific legacy service `notify.alexa_media_<device>` is different and is valid for a controlled single-device isolation probe when called directly as the service with `data.type="tts"`.
- **Never pass `notify.alexa_media*` as `entity_id`.** Alexa Media targets belong in the JSON `target` array while the normal service is `notify.alexa_media`; for the isolation probe, call the device-specific notify service directly and omit `entity_id`.
- **Never omit `data: {"type":"tts"}`.** A plain notification is not the requested spoken reply.
- **Never add titles or intros** such as “Elon says,” “Coach says,” or “Ria says” unless John explicitly requests one.
- **Never change or summarize the requested words.** If John says “answer through Alexa,” repeat the answer he is referring to, not a rewritten version.
- **Never say “it spoke” or “you heard it.”** Report command acceptance only; Home Assistant does not prove audible playback here.
- If the service fails, report the exact failed service and error. Do not convert a failed attempt into a celebratory “Done.”

## Continuous Voice Pipeline Output

For Jarvis/continuous voice replies, do not treat a disappearing HDMI sink as a reliable fallback. Configure `HERMES_ALEXA_MEDIA_PLAYER` so the enabled `voice_stack` plugin sends response text directly through `notify.alexa_media`, bypassing generated local audio.

Hermes user plugins are isolated at runtime. The voice plugin must not import Home Assistant helpers from a disabled sibling plugin merely because that import works under the source-tree `plugins.*` namespace. Keep the small authenticated REST client inside the enabled voice plugin and test its exact HTTP request contract. Also keep fake fixture hosts such as `ha.local` confined to tests; production must read and authenticate against the live `HASS_URL`.

Follow `references/continuous-voice-alexa-routing.md` for the implementation contract, isolation-aware tests, deployment ladder, and physical-hearing completion check.

## Explicit specialist forwarding

John expects an explicit destination such as `here is for running coach: <date> <shoe>` to reach the real `sportscoach` profile with its own skills, tools and credentials, just as direct chat does—not a general agent or generic worker prompted to impersonate the specialist.

- Resolve explicit specialist prefixes deterministically before semantic routing. Preserve the original request, date/shoe wording, request identity and authorization provenance; classifier output is not authorization.
- Verify which profile actually received the turn before blaming routing. A specialist's own session record is stronger evidence than its persona in a reply. Distinguish initial forwarding from delegation performed afterward by the specialist.
- When a selected specialist is unavailable, return a specific availability failure rather than silently substituting a general/raw-LLM answer.
- Check skill discovery AND referenced-file readability in the same execution context. A skill appearing in the menu does not prove its absolute references resolve. Repair the canonical installation/reference contract instead of duplicating domain instructions into voice prompts.
- Routine logging should use the specialist's existing deterministic CLI workflow. Forwarding acceptance, worker exit, database persistence and completion delivery are separate stages; do not call the request complete until persistence is verified and the reply path is accounted for.

See `references/specialist-forwarding-verification.md` for diagnostic boundaries and the regression checklist. Voice forwarding shares the profile's capabilities, but can legitimately use a separate conversation history and voice-specific delivery deadline.

## Alexa Skill Inbound Path (separate from hey_jarvis)

The cloud path Echo → Lambda HermesBridge → HA `/api/alexa` → `intent_script.HermesIntent` → `conversation.process` (agent `conversation.hermes`, `conversation_id=alexa_hermes`) → Hermes WebSocket is distinct from both outbound TTS and the voice_stack wake-word pipeline. John has corrected confusion here more than once: the Alexa skill path, the hey_jarvis wake-word path, and outbound notify.alexa_media TTS are THREE different mechanisms — never conflate them, and never "fix" one by changing another. When John reports an Alexa question got no answer or a clueless one, consult `references/alexa-skill-path-diagnostics.md` for the full payload contract, the ~8s time ceiling, the shared `alexa_hermes` conversation thread, log locations and session tags, the zombie-WebSocket-after-reconnect pitfall (TCP ESTABLISHED but adapter silent; fix is an authorized gateway restart), the budget-race slow-answer delivery design, and the last_called dynamic speaker targeting.

Debugging order matters — do NOT start in gateway.log or agent.log (they never record Alexa-skill turns; silence there means nothing). Start with `tail -50 ~/.hermes/logs/voice_events.jsonl` and look for the `heard`/`reply` pair tagged `source: "ha_assist"`:

- Both present but answer was clueless → transport worked; suspect the answer capability (post-2026-08-16 the handler is tool-capable with profile routing — check for `agent ok: false` fast-fails, which indicate a Sessions API error like duplicate session titles). Do not restart anything.
- `heard` present, no `reply`, John heard "there was a problem with the requested skill's response" → the agent overran the Alexa window; the turn should now take the slow path (ack + delayed spoken answer). If it dead-airs instead, the budget race failed.
- No `heard` entry → the turn died before Hermes: check `curl -s http://127.0.0.1:7860/health` for `active_connections: 1` FIRST.

## Pitfall: Gateway Process Env Has No HASS_TOKEN

The gateway systemd unit (`hermes-gateway.service`) has no `EnvironmentFile` and its
process environ does not include `HASS_TOKEN`/`HASS_URL`. So the voice_stack
plugin's `play_text_alexa()` — which previously did bare `os.getenv("HASS_TOKEN")` —
silently failed ("HASS_TOKEN is not configured"), which meant no wake chime when
the wake fired. Unlike `ha_conversation._get_config`, `play_text_alexa` had no
fallback. Fix (already committed, b02e486): `play_text_alexa` now falls back to
`~/.hermes/.env` via `dotenv_values(Path.home()/".hermes"/".env")` when the env is
empty, mirroring `ha_conversation._get_config`. If future voice code reads HA creds
from the process env, apply the same fallback. Keep the deployed copy at
`~/.hermes/plugins/voice_stack/pipeline.py` byte-identical to the repo copy.

## Non-Negotiable: Re-link HA After Every Gateway Restart

Restarting hermes-gateway severs HA's hermes integration WebSocket and it does NOT auto-reconnect — Alexa goes completely silent until the config entry is reloaded:

```
source ~/.hermes/.env
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" "$HASS_URL/api/config/config_entries/entry/01KZ1KWQRV4D5EQN79F9C6H8EW/reload"
```

Verify `active_connections: 1` on `http://127.0.0.1:7860/health` before declaring any gateway restart complete. A restart without this step is an incomplete change.

## Slow Sportscoach Completion vs Listening Expiry

The Alexa slow-answer callback uses generation-tagged pending state. A prior
180-second in-flight deadline silently dropped Sportscoach's final result when
an import/analysis took longer. Declarative Sportscoach completion notifications
now survive that deadline, while expired questions, DJ deadlines, superseded
requests and one-shot delivery guards remain unchanged. Result delivery is not
new user authorization and must not reopen the microphone.

Exercise the real installed `_deliver_when_done_with_target` with completed
futures and a fake monotonic clock past 180 seconds, mocking only TTS. Assert
exactly one delivery to the captured target, no old-generation delivery and no
expired-question/DJ delivery. Keep the deployed module's unrelated differences:
apply the `PendingConfirmation.deliver` hunk rather than copying an entire dirty
worktree module. Canonical scoped tests are in
`/home/john/projects/hermes-voice-ha-integration.workspace/coach-alexa-confirmation/tests/test_coach_completion_delivery.py`.

This repair restores the final slow answer; it does not yet emit a separate
notification at the database-commit instant or remove model/compression delays.
Activation still needs an explicitly authorized default-gateway restart and
HA WebSocket connection verification. A clarification timeout is not consent.
No unsolicited speaker test or historical import replay is required.

## Completion

A correct run normally needs one discovery call and one TTS service call—not terminal scripts, REST workarounds, or a small archaeological expedition.

## Canonical source and synchronization

This profile's Alexa guidance is source-owned in sodium-elon/hermes-voice-ha-integration under skills/profiles. Edit and review canonical source, merge to the fork's main, then install from its clean main checkout using scripts/sync_installed_runtime.py. Run --verify after installation. Direct installed edits create drift; reconcile them into source rather than overwriting them or treating a memory update as deployment. Keep default and Sportscoach guidance distinct.

Hermes owns the release check: main revision, passing tests, installed-file hashes, required gateway activation and bounded behavior verification. The AWS HermesBridge source is deploy/alexa/hermes_bridge/lambda_function.py in that same repository; compare its deployed code with source after an authorized Lambda update. A standard Alexa reprompt preserves the context stored by PendingConfirmation. Do not add Dialog.ElicitSlot targeting a generic query to native binary confirmation responses.

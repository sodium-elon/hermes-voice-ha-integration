---
name: alexa-voice-replies
description: "Use immediately when John says answer through Alexa, reply through Alexa, say this on Alexa, speak through Alexa, or asks for voice on Echo speakers. Use Home Assistant notify.alexa_media TTS directly; never use notify.send_message and never add an intro or alter the requested words."
version: 1.2.0
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

## Sportscoach role and protected learning

- Ordinary coaching and voice requests do not authorize coding, configuration changes, ingestion repair, or scheduled tasks.
- Use Sportscoach's own tools and skills for legitimate coaching; do not turn missing data or a lost spoken answer into unsolicited maintenance. State the limitation and answer what the available evidence supports. Engineering maintenance belongs in the owning repository workflow, only when explicitly requested.
- Load the main AlphaRunner `running-coaching` skill for coaching doctrine; do not duplicate its domain procedures here or in relay prompts. Preserve existing receipt/date/source checks, current-answer authorization and the 60-second wake expiry; this boundary adds no prerequisite gate to legitimate dispatch.
- A protected skill correction refusal is a boundary to respect, not a failed learning task to bypass. Preserve the proposed learning correction and its evidence in the conversation; state that it is not installed. Route the proposal through the owning AlphaRunner source and a reviewed PR; never bypass protection with shell writes, another profile, or an alternate tool. This handoff is a proposal, not authorization to start coding, open a PR or deploy. Voice-only guidance belongs in this voice repository instead.
- Profile capability policy is owned by AlphaRunner's `deploy/sportscoach-profile-policy.json`; voice transport does not grant tools or change profile policy. This Sportscoach boundary does not restrict the default/main profile's normal Alexa capabilities.

## Exact Workflow

1. Call `ha_list_entities(domain="media_player")` once and keep available physical Echo targets.
2. By default, target only the right Echo:
   - `media_player.john_s_echo_5th_right`
   Use the left Echo or both speakers only when John explicitly asks.
3. Send the exact requested text with this service shape:

```text
ha_call_service(
  domain="notify",
  service="alexa_media",
  data='{"message":"<exact requested text>","target":["media_player.john_s_echo_5th_right"],"data":{"type":"tts"}}'
)
```

4. Only report that Home Assistant accepted the TTS command when the result contains `success: true`.
5. Optionally re-read target states to confirm availability. An `idle` state does not prove or disprove audible playback.

## Non-Negotiable Pitfalls

- **Never call `notify.send_message`** for Alexa voice. It can return success without invoking Alexa TTS.
- **Never pass `notify.alexa_media*` as `entity_id`.** Alexa Media targets belong in the JSON `target` array while the service is `notify.alexa_media`.
- **Never omit `data: {"type":"tts"}`.** A plain notification is not the requested spoken reply.
- **Never add titles or intros** such as “Elon says,” “Coach says,” or “Ria says” unless John explicitly requests one.
- **Never change or summarize the requested words.** If John says “answer through Alexa,” repeat the answer he is referring to, not a rewritten version.
- **Never say “it spoke” or “you heard it.”** Report command acceptance only; Home Assistant does not prove audible playback here.
- If the service fails, report the exact failed service and error. Do not convert a failed attempt into a celebratory “Done.”

## Listening and follow-up failures

Use this branch only for explicitly requested engineering diagnostics or listening repair, not merely because John reports a lost spoken answer. Preserve that report, explain the known limitation and offer the owning engineering workflow without launching it. The short TTS workflow above is for one-way speech, not dialogue troubleshooting. Notification TTS does not itself open a microphone. When diagnostics are explicitly requested, follow the existing layered procedure below within the authorized scope; permission to inspect is not permission to edit, deploy or restart.

1. Preserve John's report that he answered. An absent transport event is not evidence that he failed to cooperate or stayed silent.
2. Inspect original live receipts and voice events first; use session history as secondary context. Trace request → acknowledgement → completed question → native skill Launch → answer event, retaining timestamps and provenance. Never replay a historical yes as current authorization.
3. Inspect live HA intent scripts and conversation responses before editing code. Verify Launch/HermesIntent reprompt templates and Yes/No handlers, then inspect the native `/api/alexa` response. Conversation metadata alone is insufficient: Alexa must receive a nonempty reprompt and `shouldEndSession: false`.
4. Use harmless unarmed diagnostics. Confirm no real pending question before synthetic Yes/No; a probe could otherwise consume or authorize a current action. Never arm an old import or fabricate user approval to test listening.
5. Separate HA acceptance, Alexa protocol responses, downstream intent delivery, and physical Echo capture. Passing backend tests does not prove microphone capture. Missing answer events narrow a boundary but do not establish an interaction-model defect.
6. If the backend passes, inspect the deployed Amazon skill model, locale, built-in Yes/No intents and request history using authenticated access. Do not claim a defect or fix without inspecting the relevant layer. If John must authenticate, give one small next step and wait—not a wall of console instructions.
7. Report actual changes and verification, or the exact blocker. Diagnostics alone are not a completed repair.

Protocol checks and safe reproduction: [references/native-alexa-followup-diagnostics.md](references/native-alexa-followup-diagnostics.md).

## Completion

A correct one-way TTS run normally needs one discovery call and one TTS service call—not terminal scripts, REST workarounds, or a small archaeological expedition. Explicitly requested listening repairs require the layered checks above.

## Canonical source and synchronization

This profile's Alexa guidance is source-owned in sodium-elon/hermes-voice-ha-integration under skills/profiles. Edit and review canonical source, merge to the fork's main, then install from its clean main checkout using scripts/sync_installed_runtime.py. Run --verify after installation. Direct installed edits create drift; reconcile them into source rather than overwriting them or treating a memory update as deployment. Keep default and Sportscoach guidance distinct.

Hermes owns the release check: main revision, passing tests, installed-file hashes, required gateway activation and bounded behavior verification. The AWS HermesBridge source is deploy/alexa/hermes_bridge/lambda_function.py in that same repository; compare its deployed code with source after an authorized Lambda update. A standard Alexa reprompt preserves the context stored by PendingConfirmation. Do not add Dialog.ElicitSlot targeting a generic query to native binary confirmation responses.

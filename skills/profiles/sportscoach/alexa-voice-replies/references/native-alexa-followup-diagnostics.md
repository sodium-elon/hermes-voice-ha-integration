# Native Alexa follow-up diagnostics

## Evidence-first sequence

1. Locate the original receipt and its linked voice events. Compare timestamps across heard request, acknowledgement, agent completion, skill Launch and answer receipt. Session history supplies exact generated question wording but is secondary to live transport evidence.
2. Check the deployed confirmation code, not just a source worktree. Reproduce binary-question eligibility with the exact final wording; retain profile, generation, expiry and original scope guards.
3. Inspect live Home Assistant `intent_script` entries for HermesIntent, the skill-ID Launch handler, AMAZON.YesIntent and AMAZON.NoIntent. Launch and HermesIntent should forward the conversation reprompt; Yes/No should forward their respective transport sentinel. Keep current skill IDs/configuration discoverable, not hardcoded from history.
4. Call HA `/api/conversation/process` with the Launch sentinel as a non-speaking diagnostic. Inspect `response.speech.plain.extra_data.reprompt` when that is the deployed bridge contract. This verifies the bridge response only, not the Alexa wire response.
5. Send an authenticated diagnostic LaunchRequest to HA `/api/alexa`, with a distinct diagnostic request/session ID and current timestamp. Inspect `response.shouldEndSession`, `response.outputSpeech` and `response.reprompt.outputSpeech`. This exercises HA's real Alexa response serialization without commanding speaker playback.
6. Only after establishing no live pending question, send diagnostic IntentRequests for AMAZON.YesIntent/AMAZON.NoIntent. A valid harmless response is `There is no pending question right now.` It proves endpoint routing, not Amazon speech recognition or a valid armed confirmation round trip. Do not test against a real write-authorizing pending question.
7. Inspect the authenticated Amazon console's deployed locale/model and request history. Distinguish local model edits from a built/deployed model. Missing downstream events alone do not prove missing built-in intents. Physical confirmation requires a real Echo trial and linked receipt; never manufacture that evidence.

## HA protocol mechanics verified against core 2026.9.4

- `IntentResponse.async_set_speech(..., extra_data=...)` places metadata beneath `speech.plain.extra_data`; this metadata is not automatically an Alexa reprompt.
- An `intent_script.reprompt.text` template must extract the bridge's reprompt and produce nonempty text. HA sets an actual intent reprompt only when the rendered string is nonempty.
- HA Alexa `add_reprompt` sets `shouldEndSession` to false and serializes `reprompt: {outputSpeech: ...}`. Verify the live wire response rather than assuming metadata propagation.
- Example template for the observed bridge contract: `{{ action_response.response.speech.plain.extra_data.reprompt | default('', true) }}`. Verify current bridge shape before copying it.

Authoritative implementation references (use the installed HA version):
- https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/helpers/intent.py
- https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/intent_script/__init__.py
- https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/alexa/intent.py

## Reporting boundaries

In the investigated case, the historical question was followed by a skill Launch, with no subsequent answer event. Later live diagnostic Launch returned a reprompt with `shouldEndSession: false`; unarmed Yes/No requests both reached Hermes. Those checks did not inspect Amazon's deployed model or prove physical microphone capture, so the repair remained unverified. Re-run current checks; this is an example of evidence limits, not a permanent diagnosis.

Never print HA bearer tokens or full configuration secrets. Read credentials through the existing configuration loader and report only status and relevant sanitized response fields. If console access needs user authentication, give John one small step, wait, and resume from that exact boundary.
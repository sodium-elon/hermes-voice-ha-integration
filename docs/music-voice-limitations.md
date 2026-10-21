# Music voice limitations and clarification

Text forwarded to the music profile is interpreted before the DJ session starts.
Exact known artist names and user-confirmed transcription aliases resolve without
inference. Other exclusions use bounded Jev recognition; uncertainty returns a
clarification before music research or selection. The DJ receives the original
request plus the resolved request-scoped limitation. Selection remains the DJ's
responsibility.

A clarification retains the original request for 180 seconds in the authenticated
HA conversation. A recognized answer resumes that request once; cancellation,
expiry, or a fresh full request clears it. The voice response acknowledges the
artist exclusion after the DJ session responds.

## Deployment

Deploy `plugins/voice_stack/__init__.py` and `music_request_limits.py` together and
reload the voice gateway. Merge `deploy/homeassistant/dj_yakkuza_intent.yaml` into
the existing `intent_script` configuration, validate HA configuration, and reload
intent scripts. The DJ intent must forward both speech and the reprompt; speech
alone closes the clarification exchange. Keep the existing Alexa skill model and
transport.

The installed music profile owns `references/artist-vocabulary.txt` and optional
`references/artist-transcription-aliases.json` under its music-domain skill. Aliases
are a JSON object from a user-confirmed transcription to a canonical artist name.
Preserve these private files during deployment; do not publish them or treat them
as preference evidence. A corrected transcription does not authorize a durable
dislike.

## Music model routing

The deployed music profile was restored to these model settings after a profile
configuration drift made an OpenRouter model primary:

```yaml
model:
  default: gpt-6.1-sol
  provider: openai-codex
  context_length: 100000
  persist_switch_by_default: false
```

Merge these model settings into the private profile config rather than replacing
the config or copying credentials. Retain the configured fallback chain as
secondary. Restart the music gateway after changing model settings. A temporary
model switch must not become the profile default.

The observed OpenRouter failure was HTTP 402 (credit reservation), not HTTP 429
(rate limit): the model requested up to 131072 output tokens. Restoring the
primary provider does not replenish fallback credits or alter fallback output
limits. Verify actual provider order using a fresh non-playback session and its
provider/model log entries. The deployed smoke check returned READY using one
Codex call with no tools.

## Verification boundary

Offline tests cover interpretation before handoff, exact-name recognition,
clarification continuation, expiry/cancellation, and HA reprompt rendering.
User testing confirmed the corrected incoming handoff and primary model path.
These tests do not prove audible playback or every Alexa transcription. The
separate music-preferences change supplies recent dispatch evidence and song-level
appreciation recording; this module neither ranks music nor writes taste data.

# Continuous Voice Replies Through Alexa Media Player

Use this when a wake-word/STT pipeline runs inside a Hermes user plugin and should speak replies through an Echo instead of local ALSA/PipeWire/HDMI.

## Routing contract

Configure the voice gateway with:

```ini
Environment="HERMES_ALEXA_MEDIA_PLAYER=media_player.john_s_echo_5th_right"
```

When set, send the response text directly to Home Assistant:

```text
POST <HASS_URL>/api/services/notify/alexa_media
Authorization: Bearer <HASS_TOKEN>
Content-Type: application/json

{
  "message": "<exact response text>",
  "target": ["media_player.john_s_echo_5th_right"],
  "data": {"type": "tts"}
}
```

This is a primary text-output route, not generated-audio `media_player.play_media`. Do not synthesize an MP3 first, and do not fall back to local HDMI merely because `HERMES_MEDIA_PLAYER` is empty.

## User-plugin isolation

Hermes loads user plugins under isolated names such as `hermes_plugins.voice_stack`. An enabled voice plugin cannot safely import helpers from a disabled sibling plugin such as `..home_assistant.ha_assistant`; source-tree tests under `plugins.*` can hide that production failure.

Keep the small authenticated Home Assistant REST client inside the enabled voice plugin, or import only an always-loaded core package with a proven runtime contract. Add a regression that exercises the self-contained HTTP request rather than monkeypatching a sibling plugin helper.

## Test fixture versus production URL

A fixture URL such as `http://ha.local:8123` is intentionally fake and must appear only in isolated tests. Production must read `HASS_URL`; verify the effective value independently and require an authenticated `GET <HASS_URL>/api/` to return HTTP 200 with `{"message":"API running."}` before testing Alexa TTS.

Do not infer runtime environment from `/proc/<pid>/environ` alone when Hermes loads `.env` after process start. Cross-check the gateway's successful Home Assistant connection log or execute a safe authenticated probe using the same dotenv loader/runtime.

## Bounded Alexa wake cue

To replace the local HDMI/ALSA wake beep with an Echo announcement, configure:

```ini
Environment="HERMES_WAKE_CUE_OUTPUT=alexa"
Environment="HERMES_WAKE_CUE_TEXT=Listening"
```

Reuse `HERMES_ALEXA_MEDIA_PLAYER` as the target. After wake detection, synchronously dispatch the cue through `notify.alexa_media` with `data.type="announce"` and a strict two-second HTTP timeout. Recording begins after HA accepts, rejects, or times out the dispatch; this is ordering against HA acceptance, not physical Alexa playback completion. Local playback remains fire-and-forget because sleeping HDMI can block `ffplay`.

For chime-only mode, set `HERMES_WAKE_CUE_TEXT` to an empty string. Send the physically verified punctuation-only message `.` through `notify.alexa_media` with `data.type="announce"`; on John's right Echo this produces the built-in announcement chime without speech. Do not substitute silent SSML or `media_player.play_media` sound IDs merely because Home Assistant accepts them—both were physically silent on this setup.

In Alexa cue mode, do not fall back to local `ffplay`, HDMI, or ALSA when the target or network is unavailable. Log the failure and remain silent. Add regressions proving the Alexa branch is synchronous and bounded, calls the exact target with `announce`, and never invokes local playback. Use a longer command-recording window (currently eight seconds) so Alexa latency does not consume most of the turn.

Alexa TTS dispatch also does not report when a spoken follow-up question finishes. Before issuing a follow-up cue, estimate speech duration from response length with conservative minimum and maximum bounds, then dispatch the cue synchronously and begin recording. Physically test multi-turn timing and inspect transcripts; the estimate is deliberate bounded compensation, not a fabricated playback acknowledgement.

Short place names and confirmations can decode with confidence below the normal hallucination floor even when follow-up audio RMS is healthy. Keep the normal wake-turn floor strict, but allow a separately configured lower floor only when a follow-up is already armed (for example `HERMES_STT_FOLLOW_UP_MIN_CONFIDENCE=0.02`). Log the decoded text and confidence. Do not globally lower the floor: that resurrects silence hallucinations such as “Thanks for watching.”

Do not arm follow-up listening from terminal punctuation alone. Voice agents may issue a real clarification as an imperative ending with a period, such as `Africa is vast, so name a city or country.`, or as a declarative missing-detail statement such as `For Africa, I still need a city or country.` Recognize explicit question marks, a narrow set of clarification directives (`name`, `choose`, `specify`, `confirm`, `tell me`, etc.), and narrowly scoped missing-information nouns (`city`, `country`, `location`, `answer`, `confirmation`, `details`). Add exact production phrases as regressions and retain factual/operational negative controls such as `I still need to install the weather integration.` to avoid turning every sentence containing “need” into another recording window.

## Deployment and verification ladder

1. Write a RED test proving the configured Alexa target bypasses local audio synthesis/playback.
2. Write a request-contract test asserting URL, bearer header, timeout, exact target array, and `data.type="tts"`.
3. Run the targeted test, then the full plugin suite.
4. Copy the tested files into `$HERMES_HOME/plugins/voice_stack/`; verify source and installed SHA-256 hashes match.
5. Add the systemd environment variable and run `systemctl --user daemon-reload`.
6. Restart only the affected gateway with explicit authorization.
7. Verify a new PID, active/running state, zero restart loop, expected environment, and required listening ports.
8. Exercise the installed helper with a harmless sentence and require success.
9. Trigger one real wake-word turn. Inspect the production voice event log for `spoken` with the Echo target; `speak_failed` or API acceptance without audible playback is not completion.
10. Ask John what he physically heard. His ear is the final playback oracle.

## Failure signature

If production logs say `HA bridge not available — cannot use Alexa TTS` while source-tree tests pass, suspect sibling-plugin isolation first. Replace the sibling import with the self-contained authenticated REST path, add the isolation-aware request-contract test, resync, restart under authorization, and repeat the live wake-word probe.

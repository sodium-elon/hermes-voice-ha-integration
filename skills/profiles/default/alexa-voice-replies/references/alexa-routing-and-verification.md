# Alexa routing and physical verification

## Correct Home Assistant call

Use Alexa Media TTS, not generic notify delivery:

```text
ha_call_service(
  domain="notify",
  service="alexa_media",
  data='{"message":"<exact text>","target":["media_player.john_s_echo_5th_right"],"data":{"type":"tts"}}'
)
```

A device-specific legacy service such as `notify.alexa_media_john_s_echo_5th_right` can also be used as an isolation probe. Never substitute `notify.send_message`; it may return success without spoken TTS.

## Acceptance is not playback proof

`success: true` proves only that Home Assistant accepted the service call. It does not prove:

- that Alexa produced sound;
- which physical speaker produced it;
- that a single targeted entity stayed isolated;
- that the exact words were audible.

Report acceptance precisely, then rely on John's physical observation when speaker isolation matters.

## Stereo pairs and Amazon-side grouping

A single Home Assistant media-player target can still sound through two physical Echos when Amazon has them configured as a stereo pair, home-theater pair, or speaker group. Even a device-specific Alexa Media service may follow Amazon's grouping.

When John changes the default speaker or reports sound from the wrong devices:

1. Send a short distinctive phrase to the requested entity using `notify.alexa_media` with `type: tts`.
2. If isolation is disputed, repeat once through the device-specific Alexa Media service.
3. Ask which physical speaker(s) were heard; HA state commonly remains `idle` and cannot answer this.
4. If both paths play through both speakers, inspect the Alexa app's stereo pair/home-theater/speaker-group configuration. Do not unlink or reconfigure the pair without explicit permission.
5. Keep the requested logical target in the skill, but never claim physical isolation until John confirms it.

## Truthful completion language

Good: “Home Assistant accepted TTS for the right Echo. Which speaker did you hear?”

Bad: “The right Echo spoke,” “message delivered,” or “you should have heard it.”

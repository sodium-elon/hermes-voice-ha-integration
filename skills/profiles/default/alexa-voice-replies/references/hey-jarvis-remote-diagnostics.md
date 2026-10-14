# Remote diagnostics for `hey_jarvis`

Use this when John asks whether `hey_jarvis` was heard, reports inconsistent wake detection, or is away from the physical satellite.

## Keep the mechanisms separate

- Alexa inbound skill: Echo microphone → Amazon/Lambda → Home Assistant → Hermes.
- `hey_jarvis`: sound physically reaching the Tungsten microphone → OpenWakeWord → wake listener → Palladium bridge → Hermes voice stack.
- Alexa outbound TTS: Home Assistant tells an Echo to speak; the Echo is only a loudspeaker.

An Echo does not natively listen for `hey_jarvis`. A remote phone or Echo cannot trigger Tungsten unless its sound is physically audible near Tungsten.

## Presence check comes first

Before coordinating a human capture, ask whether the user is physically near the microphone. Do not interpret a silent recording as microphone failure when nobody was present. This check precedes gain changes, service restarts, ALSA bypass tests, and login-dependency theories.

If the user is remote, identify which Echo is physically near Tungsten. A controlled Alexa TTS of the exact phrase can exercise the room-acoustic wake path, but it is a synthetic path test—not a substitute for human speech-quality validation.

## Evidence ladder

1. Check recent Tungsten wake logs for model name, score, and timestamp.
2. Check wake-listener forwarding at the same timestamp.
3. Check Palladium's wake-bridge receipt.
4. Check `voice_events.jsonl` for `wake`, then `record`, then `heard`/`no_speech`/`discarded`.
5. Normalize UTC versus CEST before saying whether an event was “a minute ago.”

Interpret boundaries precisely:

- Wake score above threshold + bridge receipt: wake path worked.
- Wake followed by `no_speech`: phrase was detected, but the post-wake command was not transcribed.
- No wake log anywhere: phrase did not cross the model threshold.
- Nonzero audio bytes or RMS alone do not prove intelligible speech.

## Missing-chime triage

Treat “Hey Jarvis no longer wakes with a chime” as two separate hypotheses: the wake event never occurred, or the event occurred and cue playback failed. Do not debug Alexa/local playback until the evidence ladder proves a fresh wake event.

1. Compare the user's test time with the latest genuine detection in both Tungsten's wake-service journal and Palladium's `voice_events.jsonl`. A running listener with an old last-detection timestamp is not a healthy end-to-end wake path.
2. Inspect the running gateway process environment—not only the systemd drop-in—for `HERMES_WAKE_CUE_OUTPUT`, `HERMES_WAKE_CUE_TEXT`, `HERMES_ALEXA_MEDIA_PLAYER`, and `HERMES_WAKE_BEEP_DURATION`.
3. Interpret cue configuration accurately:
   - `HERMES_WAKE_CUE_OUTPUT=local` uses the generated local beep and must be checked against the effective Pulse sink.
   - `HERMES_WAKE_CUE_OUTPUT=alexa` uses Alexa Media Player. In the current voice-stack implementation, blank `HERMES_WAKE_CUE_TEXT` becomes `"."` with notification type `announce`, relying on Alexa's announcement chime rather than spoken cue text.
4. If there is no fresh wake detection, state plainly that the cue code was never reached and continue with production-microphone/model diagnosis.
5. If a fresh wake exists but no chime was heard, independently exercise the configured cue route and require the user's physical observation; service acceptance is not proof of sound.
6. Check deployed and workspace plugin hashes when source and runtime behavior may differ. A modified file does not affect an already-running Python process until an explicitly authorized gateway restart.

## Remote synthetic test

When an Echo is known to be physically near Tungsten:

1. Start a bounded production-path recording for evidence.
2. Send exact TTS `Hey Jarvis` through the device-specific Echo notify service.
3. Inspect model score, listener forwarding, bridge receipt, and voice event sequence.
4. Do not claim human wake-word quality from this test; speaker TTS acoustics differ from a person.

## Gain and threshold diagnosis

Measure RMS, peak, and clipped-sample ratio through the exact production source. Inspect both PipeWire volume and ALSA `Capture`/`Internal Mic Boost`; a superficially ordinary 100% PipeWire setting can map to extreme positive hardware gain and badly clip speech.

A high clipped ratio proves overdrive, but does not by itself prove that clipping caused a missed wake. Confirm causality with an A/B recording containing the same real spoken phrase at preserved original and reduced gain. Restore the original level automatically with a trap unless the user authorizes persistence.

After obtaining a clean reduced-gain capture:

1. Transcribe the preserved WAV to prove the phrase is intelligible.
2. Score that same WAV with the exact deployed OpenWakeWord model, ONNX backend, 1280-sample frames, VAD threshold, and detection threshold.
3. Separate phrase-time scores from quiet-room scores. Never lower the threshold merely because one phrase missed.
4. If speech is intelligible, clipping is zero, the phrase score lands just below threshold, and ambient scores remain far below it, propose one small threshold change with explicit values.
5. After authorization, edit the persistent wake-service configuration, restart only the wake service, verify its effective `ExecStart`, and confirm the listener reconnects before asking for another human test.
6. A clean recording, active services, and valid configuration are not completion. Require a fresh production detection, Palladium `wake` event, and John's physical report of the chime.

When a previously proven gain has drifted after reboot or session changes, diagnose the persistence mechanism separately. Restoring gain fixes the immediate audio path; it does not prove the setting will survive another reboot.

### Test VAD separately from the wake threshold

Do not assume a clean, intelligible phrase with a low wake score means the final wake threshold is the only remaining control. Replay the same preserved WAV through a differential matrix using:

- the current production VAD threshold;
- one or two lower VAD thresholds;
- VAD disabled as a diagnostic control.

A high VAD threshold can zero every wake score even when Whisper clearly transcribes “Hey Jarvis.” If production VAD yields `0.0` while a lower VAD restores a strong score, tune VAD first; do not keep ratcheting down the wake threshold. Compare phrase-window maxima with quiet-room maxima before proposing values.

Use the preserved WAV as a deterministic red/green test: current production arguments must fail, while candidate VAD + wake-threshold arguments must cross the detection boundary on the same audio. After authorization, restart only the wake service, verify effective `ExecStart`, and verify the listener reconnects.

## Wake succeeds but the command is missing

The production path is two-stage:

1. Tungsten detects the wake phrase and notifies Palladium.
2. Palladium then opens a new recording window for the command.

Wake-word audio is not buffered into Palladium’s command recording. A one-breath command such as “Hey Jarvis, what time is it?” can therefore finish before the second-stage recorder opens. The decisive signature is:

- Tungsten logs a score above threshold;
- the listener forwards the detection;
- Palladium emits `wake`, then `record`;
- the turn ends as `no_speech` / `empty_transcript`.

Describe this accurately: the system did not misunderstand the command; it never recorded it.

For a controlled verification, start any diagnostic capture before instructing the user, then ask them to say the wake phrase, pause about two seconds, and speak the command. If that works, the durable product choices are:

- provide a physically verified audible “Listening” cue; or
- redesign the satellite/bridge to preserve post-wake audio so natural one-breath commands work.

An Alexa announcement containing only `"."` is not a reliable audible cue. Home Assistant accepting the notify request does not prove the user heard a chime. A synchronous Alexa cue can also delay command capture, so compare cue-dispatch and recording-start timestamps rather than assuming the cue improves timing.

Never infer a pre-login hardware failure from captures made while the user is absent. That is absence dressed up as diagnostics.

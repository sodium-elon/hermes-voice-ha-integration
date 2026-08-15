# Voice STT calibration

How the mic gain and wake-word behavior were calibrated on 2026-08-15, and
how to re-verify it.

## The rig

- Mic + speakers live on a separate machine from the gateway (called
  *Tungsten* below; substitute your own `$MIC_HOST`), reached over
  PulseAudio TCP (`PULSE_SERVER=tcp:$MIC_HOST:4713`).
- Capture chain: ALC269VC internal mic → ALSA Capture +30 dB → PipeWire
  source `alsa_input.pci-0000_c6_00.6.analog-stereo` → gateway records
  16 kHz mono float32 via sounddevice.
- Playback chain (TTS replies): edge-tts → ffplay → default sink (HDMI/TV).

## Symptoms that led here

`~/.hermes/logs/voice_events.jsonl` (read `hermes-voice-monitor`) showed:

- Real commands heard at confidence ~0.2 (`Turn on the kitchen light`).
- Whisper hallucinations on ambient TV audio: `Thanks for watching!`,
  `I'm Chris.`, `All right.`, `Bye-bye.`
- Historic mic recordings (`~/.hermes/voice_debug/`) either square-wave
  clipped (RMS 0.85–0.91) or nearly silent (RMS 0.008).

## Root cause

PipeWire source volume mis-set:

| source vol | loud speech peak | result |
|---|---|---|
| 70% | 1.3–1.5 (clipped) | transcripts collapse |
| 50% | 1.3–1.4 (clipped) | 3/3 loud OK, quiet fails |
| 41% (was live) | 2.0–2.2 (hard clipped) | `kitchen` → `engine` etc. |
| **30% (calibrated)** | **≤1.0** | **19/20 loud, mean conf 0.59** |

## The harness

`scripts/voice_stt_calibration.py` synthesises 21 test phrases with the
runtime TTS voice (edge-tts `en-US-AriaNeural`), then transcribes them with
the plugin's `FasterWhisperEngine` under the live gateway config:

```bash
VENV=~/.hermes/hermes-agent/venv/bin/python

# lab battery (clean, gain-scaled, noise-mixed) — no mic needed
$VENV scripts/voice_stt_calibration.py --snr 15 5

# air-gap battery: speakers → room → real mic (the real test)
export PULSE_SERVER=tcp:$MIC_HOST:4713
$VENV scripts/voice_stt_calibration.py --acoustic --play-volume 100
$VENV scripts/voice_stt_calibration.py --acoustic --play-volume 40

# transcribe historic recordings
$VENV scripts/voice_stt_calibration.py --real ~/.hermes/voice_debug

# compare whisper decode options
$VENV scripts/voice_stt_calibration.py --sweep
```

## Fixes applied

1. **Capture source volume 41% → 30%** (`scripts/mic-calibration.sh`
   re-applies it). Live immediately, no gateway restart needed.
2. **`plugins/voice_stack/engines/stt.py`**: decode each utterance
   independently (`condition_on_previous_text=False`, kills hallucination
   loops) and expose `no_speech_threshold` / `log_prob_threshold` gates
   (env: `HERMES_STT_NO_SPEECH_THRESHOLD`, `HERMES_STT_LOG_PROB_THRESHOLD`).
   Synced to `~/.hermes/plugins/voice_stack/` — **takes effect on next
   gateway restart** (backup in `~/.hermes/backups/stt-calibration-*`).

## Known limits

- When the room TV is talking, its speech mixes into the record window and
  Whisper will sometimes prefer it. Gain cannot fix a competing talker;
  the durable fix is ducking/pausing the TV on wake-word (HA automation).

## Wake-word rewake calibration (2026-08-15)

The assistant kept re-waking on its own TTS replies: `spoken` was followed
by `wake` within 1s (scores 0.75–0.98) → record → answer → repeat. Research
(openwakeword README, wyoming-satellite `--wake-refractory-seconds`, ESPHome
voice-assistant hooks) pointed at a layered fix, all applied:

| Layer | Setting | Offline evidence |
|---|---|---|
| Refractory cooldown | `HERMES_WAKE_COOLDOWN=5.0` — no wake detection for 5s after TTS playback ends | was the rewake killer; matches wyoming-satellite's design |
| Threshold | `HERMES_WAKE_WORD_THRESHOLD` 0.40 → **0.55** (model trained for 0.5) | real wake clip scores 0.95–0.99 offline; acoustically 0.73–0.81 |
| Silero VAD gate | `HERMES_WAKE_VAD_THRESHOLD=0.5` (bundled `silero_vad.onnx`) | ambient TV: max 0.030 → **0.000**; real wake unchanged at 0.99 |
| Model state reset | `Model.reset()` on each fresh listen | stale melspectrogram features can't bleed across turns |

End-to-end confirmation (speakers → mic, live gateway): wake fired at 0.812,
command heard `Turn on CNN.` conf 0.556, reply spoken — and **no wake for the
full window after** (previously: wake within 1s of `spoken`).

Live config lives in the systemd drop-in
`~/.config/systemd/user/hermes-gateway.service.d/voice-stack.conf` — after
editing, `systemctl --user daemon-reload && systemctl --user restart
hermes-gateway.service`.

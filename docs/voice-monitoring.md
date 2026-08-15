# Watching the voice pipeline

`hermes-voice-monitor` is a temporary debugging window onto a live voice
interaction: what the wake word heard, which route handled it, which services
and tools fired, and what was spoken back. Attach it when something misbehaves,
read the conversation, press Ctrl-C.

```bash
hermes-voice-monitor              # follow live
hermes-voice-monitor --since 30m  # replay the last half hour, then follow
hermes-voice-monitor --since 2h -n  # replay only, then exit
hermes-voice-monitor --all        # include unmatched tool calls and unknown events
hermes-voice-monitor --json       # raw merged stream, for piping into jq
```

Typical output:

```text
── turn 12 ─────────────────────────────────────────────────────
10:41:03 ◉    wake  hey_jarvis_v0.1  score 0.97
10:41:03 ·          recorded 2.8s  (rms 0.0331) +0.4s
10:41:04 🎤   heard  "is the front door locked"  (conf 0.88, stt 0.61s)
10:41:04 ⚡   route  Home Assistant intent → lock.front_door
10:41:04 💬  hermes  "Yes, the front door is locked."  (thought 0.42s)
10:41:05 ·          spoken via TTS → local speakers  (0.44s) +2.1s
```

## Where the data comes from

The monitor is a pure reader — no port, no daemon, no service to keep running.
It merges two files by timestamp:

| Source | Contributes |
|---|---|
| `~/.hermes/logs/voice_events.jsonl` | pipeline stages, written by `voice_stack/events.py` |
| `~/.hermes/ha_audit.log` | Home Assistant service calls Hermes made (`🔧 did`) |

Tool calls (`🔩 tool`) arrive on the Sessions API SSE stream as the agent makes
them, carrying the arguments each tool was called with. They are not
reconstructed from `agent.log`, so nothing has to be correlated by session id
and nothing is lost when that log rotates mid-turn. Pass `--all` to also see
tool completions and token usage.

## Prerequisite: the Hermes API server

The tool-capable fallback (anything Home Assistant cannot match itself) runs
through the gateway's `api_server` platform. Enable it once:

```yaml
# ~/.hermes/config.yaml
platforms:
  api_server:
    enabled: true
    extra:
      host: 127.0.0.1     # loopback only
      port: 8642

platform_toolsets:
  api_server:
    - homeassistant
    - web
```

```bash
# ~/.hermes/.env — gates the API server; generate with `python -c
# "import secrets; print(secrets.token_urlsafe(32))"`
API_SERVER_KEY=…
```

If the API server is unreachable the voice stack degrades to a raw LLM
completion with no tools, exactly as it did when the fallback shelled out.

## Event kinds

| Kind | Meaning |
|---|---|
| `wake` | wake word fired — engine, model, score, threshold |
| `record` | microphone captured audio — duration and RMS |
| `no_speech` | woke but captured nothing usable — the signature of a false trigger |
| `heard` | STT transcript, confidence, and how long transcription took |
| `discarded` | transcript rejected below `HERMES_STT_MIN_CONFIDENCE` |
| `route` | which side handled it: a native HA intent, or escalation to Hermes |
| `agent_run` | the agent turn started — run id |
| `tool` | a tool call, with phase (`started`/`completed`/`failed`) and its arguments |
| `usage` | token counts reported at the end of the run |
| `agent` | the agent turn finished — session id and wall time |
| `reply` | the text that will be spoken |
| `spoken` / `speak_failed` | TTS synthesis and playback outcome |
| `voice_action` | `voice_enable` / `voice_disable` / `voice_status` from Home Assistant |
| `error` | any stage that threw |

A turn starts at `wake` (or at an inbound `assist_query` from HA Assist) and the
monitor groups everything under it until the next one.

## Cost and switching it off

Emission is best-effort and wrapped so it can never break the voice pipeline: a
few hundred bytes appended per interaction, rotated at 5 MB. Because it is
always recording, you can attach the monitor *after* the odd behaviour and still
replay it with `--since`.

To disable entirely:

```bash
systemctl --user edit hermes-gateway.service   # add HERMES_VOICE_EVENTS=false
```

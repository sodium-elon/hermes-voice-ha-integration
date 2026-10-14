# Alexa Skill Inbound Path: Contract and Log Diagnosis

Distinct from hey_jarvis (voice_stack wake-word pipeline) and from outbound
`notify.alexa_media` TTS. This is the cloud path: user invokes the Alexa
skill and the spoken answer is Hermes' text response.

## The Path

1. Echo → Alexa cloud. Invocation name `hermes assistant`. Shapes:
   `LaunchRequest`, or `IntentRequest` → `HermesIntent` with slot `query`
   (AMAZON.SearchQuery). Locale models en-US/GB/CA/AU/IN are identical.
2. Alexa → Lambda `HermesBridge` (eu-west-1). Empty query: Lambda answers the
   elicit prompt itself and never contacts HA.
3. Lambda → HA: `POST https://hello.anthood.net/api/alexa`,
   `Authorization: Bearer <session.user.accessToken>`, body = the entire
   unmodified Alexa event.
4. HA `alexa` → `intent_script.HermesIntent` → `conversation.process` with
   `agent_id: conversation.hermes`, `text: "{{ query }}"`, `language: en`,
   `conversation_id: alexa_hermes`.
5. `conversation.hermes` → WebSocket to Hermes gateway. Sends
   `assist_query`, awaits a Future keyed on `conversation_id`, timeout 30s.

Inbound payload Hermes receives (this is ALL of it — no device id, locale,
user, room):

```json
{"type":"assist_query","text":"...","conversation_id":"alexa_hermes","language":"en"}
```

Required reply (echo conversation_id verbatim or the Future hangs 30s):

```json
{"type":"assist_response","conversation_id":"alexa_hermes","text":"..."}
```

`speech.plain.speech` also accepted. `hermes_event`, `state_ack`,
`state_changed`, inbound acks are accepted and ignored.

## Hard Constraints

- Effective time budget ~8s end-to-end (Alexa cuts first; Lambda 10s; bridge
  30s). Overrun = dead session, not a speakable error. Single fast lookups
  only; slow work must ack immediately and deliver elsewhere.
- One global conversation thread: `alexa_hermes` is hardcoded, shared across
  every Echo and every person. Follow-ups work; per-room/per-user separation
  does not exist. Be careful with stateful/destructive tools.
- Reply is spoken and is the ONLY output channel: no markdown, lists, URLs,
  cards, APL, or Alexa directives from Hermes.
- `tts_engine`/`tts_voice`/`stt_engine`/`wake_word` options on the HA Hermes
  integration apply to HA's own Assist pipeline, NOT this path — Amazon does
  STT and TTS here, which is why answers come back in the Echo's own voice.

## Log Diagnosis

FIRST stop: `~/.hermes/logs/voice_events.jsonl` — the authoritative log for
Alexa-skill turns. Each turn appears as a `heard`/`reply` pair:

```json
{"kind": "heard", "source": "ha_assist", "text": "...", "conversation_id": "alexa_hermes", "language": "en"}
{"kind": "reply", "source": "ha_assist", "text": "...", "think_seconds": 4.9}
```

- `heard` with `source: "ha_assist"` = the assist_query reached Hermes.
- `reply` with `source: "ha_assist"` = the assist_response was sent back.
- hey_jarvis turns look different: `kind: "wake"`, `"record"`, `heard` with
  `confidence`/`stt_seconds` and NO `source: ha_assist`, plus `spoken` with
  `sink: media_player.john_s_echo_5th_right`.

CRITICAL: gateway.log and agent.log do NOT record Alexa-skill turns. Grepping
them for "alexa"/"assist_query" and finding nothing is expected — it is NOT
proof the turn never happened. Only absence of a `heard` entry in
voice_events.jsonl proves the turn never reached Hermes.

If both `heard` and `reply` exist but John says "she had no idea what I was
talking about": the transport WORKED and the failure is answer capability,
not plumbing. Do NOT restart gateways or chase WebSockets for this.

RESOLVED 2026-08-16: the assist_query handler
(voice_stack/__init__.py :: _handle_assist_query_with_llm) now routes through
the tool-capable Sessions API agent (_run_full_agent) instead of a bare
ctx.llm.acomplete, with _voice_profile_for profile routing (running-data
questions -> sportscoach profile on port 8643) on a dedicated alexa_hermes
session titled "Voice (Alexa)". hey_jarvis ("voice-stack" session,
_route_voice_transcript) was deliberately left untouched — the two paths
share helpers but keep separate sessions/histories. John explicitly demanded
this separation; never "unify" them.

## Explicit running-log forwarding

`here is for running coach: <date> <shoe>` is a deterministic, case-insensitive
prefix route to the real Sportscoach Sessions API (default :8643), not a Jev
classification or a generic agent impersonation. The original request remains
unchanged; Alexa keeps its own `alexa_hermes` history, separate from Hey Jarvis.
Sportscoach owns the fixed coach CLI workflow and verified final result, not a
routine generic delegated worker. Domain instructions belong in Sportscoach's
skills, not duplicated in the relay prompt.

Sportscoach API failure returns a deliverable unavailable message; an empty
completion explicitly says the import is unconfirmed. Neither substitutes a
contextless general LLM, and both remain deliverable after the slow-path ack.
Physical hearing and a verified DB import remain separate evidence requirements.

When fixing this path, compare installed plugin against workspace before copying:
unrelated unfinished native-Alexa work can coexist with the deployed busy/one-shot
confirmation path. Apply scoped hunks and test the actual installed module rather
than overwriting that runtime with the whole dirty workspace file.

## Budget Race and Slow-Answer Delivery (deployed 2026-08-16)

The tool-capable agent routinely exceeds the 8s Alexa budget on multi-hop
tool chains (one garmin.sqlite run question explored for 30s+; Alexa spoke
"there was a problem with the requested skill's response" while Hermes was
still mid-query — that error string means the Lambda/HA Future timed out,
NOT that Hermes errored). The handler therefore races the agent:

- ALEXA_AGENT_BUDGET_SECONDS (env HERMES_ALEXA_AGENT_BUDGET, default 6.5s).
- Fast path: agent finishes in budget -> answer returns over the WebSocket,
  Alexa speaks it natively. No done-callback attached -> no duplicate speech.
- Slow path: the WS reply is the ack (ALEXA_SLOW_ACK, env
  HERMES_ALEXA_SLOW_ACK) which Alexa speaks natively on the asking device.
  The agent thread keeps running; a done-callback speaks the real answer via
  notify.alexa_media when it completes.

Implementation pitfalls in the race (all bit during development):

- Sessions API enforces unique session titles server-wide. Creating a
  second session titled "Voice (Hey Jarvis)" fails HTTP 400 invalid_title.
  Every new voice surface needs its own session id AND title, threaded
  through ensure_session(name, title) -> complete(session, session_title) ->
  _run_full_agent(session_title=...). Symptom in voice_events.jsonl: an
  agent event with runner "sessions api", ok false, seconds under 0.1 — a
  sub-100ms fast-fail — followed by a slow bare-LLM fallback reply
  (think_seconds well over the 8s Alexa budget). The real error only
  appears in gateway.log as "Alexa tool-capable agent failed; falling back
  to raw LLM: session create failed: HTTP 400 ... invalid_title".
- asyncio.wait_for cancels its wrapper on timeout. The answer must be
  recovered from the RAW concurrent.futures future submitted to a dedicated
  ThreadPoolExecutor (ALEXA_EXECUTOR), wrapped with asyncio.wrap_future(cf)
  only for the await. Hold cf, attach cf.add_done_callback(...) in the
  TimeoutError branch — calling .result() on the cancelled asyncio wrapper
  raises CancelledError and silently discards the finished answer.
- Attach the done-callback only AFTER the timeout, never before the race:
  attaching up front makes the fast path speak twice (WS reply AND
  notify.alexa_media).
- Never re-speak the ack via notify.alexa_media. Alexa already speaks the
  WS ack natively on the asking device; a notify duplicate lands on the
  wrong speaker (observed: ack spoken twice, second one on the hardcoded
  right Echo).
- Blocking requests-SSE streaming must not run on the ws_receiver's
  aiohttp event loop. _handle_assist_query_with_llm is async; the agent
  call goes through the executor or every concurrent HA WS message stalls
  behind one Alexa turn.
- A bare-LLM fallback remains for general requests, but it routinely exceeds
  the 8s budget — dead air. Sportscoach-owned requests must never use that
  contextless fallback: failed/empty owner completions produce explicit
  deliverable failure text, including after the slow-path acknowledgment.

## Wrong-Speaker Fix: last_called Targeting (deployed 2026-08-16)

The inbound payload carries no device id — Hermes cannot know which Echo
asked. Hardcoded HERMES_ALEXA_MEDIA_PLAYER delivery answered on the right
Echo even when John asked from another device. Fix: at timeout, resolve the
asking Echo via the alexa_media integration's last_called attribute (Amazon
flags the device it last interacted with — at query time, the one just
spoken to):

    POST {HASS_URL}/api/template   (Authorization: Bearer $HASS_TOKEN)
    template: {{ states.media_player | selectattr('attributes.last_called','eq',true) | map(attribute='entity_id') | join(',') }}

Resolve ONCE at ack time (the flag is still fresh; later speech would flip
it), pass the resolved target to the done-callback, fall back to
HERMES_ALEXA_MEDIA_PLAYER when the lookup fails. hey_jarvis still uses the
static HERMES_ALEXA_MEDIA_PLAYER sink — dynamic targeting is an
Alexa-skill-path feature only.

## Pitfall: Gateway Restart Severs HA's Hermes Integration WS

After EVERY systemctl --user restart hermes-gateway, HA's hermes custom
integration (config entry 01KZ1KWQRV4D5EQN79F9C6H8EW, pointed at the
Tungsten proxy 192.168.86.77:7860) does NOT auto-reconnect its WebSocket.
The receiver listens on 7860 but active_connections stays 0 and every Alexa
turn silently dies. Observed 2026-08-16: "Hermes is not responding" right
after a restart was exactly this.

Recovery after every gateway restart (one command):

    source ~/.hermes/.env
    curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" \
      "$HASS_URL/api/config/config_entries/entry/01KZ1KWQRV4D5EQN79F9C6H8EW/reload"

Then verify: curl -s http://127.0.0.1:7860/health must show
active_connections: 1 (and ss -tnp | grep 7860 shows the ESTABLISHED
connection from Tungsten).

Secondary logs (plumbing failures only — i.e. no `heard` in voice_events.jsonl):

- `~/.hermes/logs/gateway.log` — HA adapter reconnect churn, `ha_events`
  inbound pushes, voice_stack pipeline entries.
- `~/.hermes/logs/agent.log` — per-session turns. Tag tells the path:
  - `platform=homeassistant chat=ha_events` = HA event pushes (not Alexa).
  - `[voice-stack] platform=api_server` = hey_jarvis wake-word turns
    (also logged as `hermes_plugins.voice_stack.pipeline: Voice input:`).
- `journalctl --user -u hermes-gateway` — same adapter lines; can lag the
  file logs, so check both.

## Pitfall: Zombie WebSocket After Reconnect

Observed 2026-08-16: the homeassistant adapter logged `Reconnected` at
18:44:48, then went completely silent while `ss -tnp` still showed the TCP
socket to HA `ESTABLISHED`. Alexa turns never arrived (zero log entries) even
though the connection looked alive — the event subscription had died silently
and the adapter does not verify or re-subscribe after reconnect.

Detection recipe:
1. `grep -n "Reconnected" ~/.hermes/logs/gateway.log | tail -1` — note the
   timestamp.
2. If NO homeassistant entries exist after that timestamp while the user
   reports voice turns that should have arrived, suspect zombie state.
3. Confirm socket: `ss -tnp | grep 192.168.86.78` — ESTABLISHED does NOT
   prove the WS subscription is alive.
4. Fix: restart the gateway (`systemctl --user restart hermes-gateway`) —
   ONLY with John's explicit authorization, per profile-scoped gateway rules.

Related port-conflict symptom: `voice_stack.ws_receiver` failing with
`[Errno 98] address already in use` on port 7860 means a stale gateway
process still holds the port; check `lsof -i :7860` before restarting
anything.

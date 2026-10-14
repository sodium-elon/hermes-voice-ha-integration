# Running log forwarding through Home Assistant Assist

`here is for running coach: <date> <shoe>` is an explicit destination, not a
classification problem. `_voice_profile_for` recognizes this leading prefix
(case-insensitive, whitespace-tolerant) before Jev. The Assist handler forwards
the original text unchanged to the real `sportscoach` Sessions API (default
loopback port 8643). Each new specialist request gets a fresh
`alexa_hermes-<uuid>` session; a bounded yes/no answer reuses the saved
question session, original request and speaker target. This keeps
the profile's tools, skills and credentials; it does not imitate Sportscoach in
the general agent or combine Alexa and Hey Jarvis histories.

Routine imports remain the main Sportscoach's fixed coach CLI workflow, with
explicit date/shoe identity, durable authorization and actual DB read-back.
Forwarding a request is not evidence of a successful import. The profile skills
own the domain procedure; the relay must not duplicate it in a channel prompt.

Sportscoach transport errors return `Sports Coach is unavailable right now.`
and empty completions return `Sports Coach returned no completion. The run
import is not confirmed.` Both are deliverable text even after the Alexa budget
has expired, rather than exceptions silently discarded by the slow callback.
The Assist handler does not substitute a contextless general LLM for a failed
Sportscoach request. Music/general routing is unchanged.

## Regression verification

Use the established Hermes interpreter, with no real audio, inference or import:

```
/home/john/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_assist_routing.py tests/test_running_coach_relay.py \
  tests/test_sessions_api.py -o addopts= -q
```

The relay tests exercise original text preservation, owner/session forwarding,
API failure, empty completion, and one delayed failure delivery after the ack.
They do not prove physical speaker playback. Do not replay a logging request
against production simply to test routing; it authorizes an import.

Deploy from the clean release `main` checkout using the managed installation
procedure in [runtime-source-sync.md](runtime-source-sync.md). Preserve feature
work until its useful changes are merged or it has been explicitly retired.
Installed-only patches are not a completed release.

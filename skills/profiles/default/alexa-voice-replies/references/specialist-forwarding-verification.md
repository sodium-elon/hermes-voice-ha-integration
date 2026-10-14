# Specialist forwarding: prove each boundary

## Contract

An explicit destination such as `here is for running coach: <date> <shoe>` is routing information, not a question for an intent model. Select the real specialist profile before semantic classification. Preserve user text and transport provenance; do not manufacture confirmation or substitute an impersonating generic worker. A separate voice session is acceptable: equivalent capabilities do not imply shared Telegram history.

## Investigation order

1. Identify the surface: Alexa skill/HA Assist, Jarvis wake-word, or outbound TTS. Read the inbound `heard` event and exact payload first.
2. Trace the router and Sessions API endpoint selected. Confirm the receiving profile through its actual session store. Do not infer profile identity from response style or a task prompt.
3. Separate the receiving specialist turn from any child it launches. A request can route correctly and fail later in delegation, tool execution or completion delivery.
4. Validate the skill contract in the receiving AND child context: discovery path, SKILL.md readability, linked reference readability, executable and token-store configuration, and state-directory consistency. Test those relationships rather than memorizing today's missing paths. Canonical project skills and profile-installed references must not point into incompatible trees.
5. For a failed subprocess, retain bounded sanitized exit status and stderr. `Garmin command failed: auth check` proves command failure, not expired credentials. Compare the exact wrapper invocation with the canonical direct CLI invocation and environment. Recover authentication only when the evidence warrants it; do not switch token stores or to a retired transport.
6. Inspect durable task state and live database read-back before retrying a write. A generated compaction summary claiming completion is not evidence. Child exit code zero is not evidence of persistence.
7. Trace reply completion separately from import completion. An immediate acknowledgment is not the final coaching reply. Preserve the asking-device target and existing voice deadlines; require physical hearing only for an authorized voice test, never unsolicited playback.

## Regression checklist

- Explicit coach prefix selects Sportscoach without a Jev call, including when classification is unavailable.
- The real specialist endpoint receives the original payload; no general-agent impersonation or silent fallback.
- A specialist outage yields an honest availability error.
- All required skill and reference paths resolve in the actual runtime context.
- Routine logging invokes the existing domain CLI, not generated scratch import programs or untracked background delegation.
- Subprocess errors retain useful sanitized causes without leaking credentials.
- Confirmed scope and task identity survive retries/compaction; retries cannot duplicate an import.
- Persistence is independently verified, and both fast-response and delayed-response delivery paths have tests.

## Scope

These are diagnostic and implementation requirements, not proof that the runtime has been repaired. Inspect current code and configuration before changing anything. Keep code/profile edits scoped and obtain explicit authorization for gateway restarts. Use fake import fixtures for regression tests; do not replay a live run write merely to test routing.

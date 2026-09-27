---
name: music-domain
description: DJ Yakkuza workflow for music requests, recommendations, playback state, and durable personal preference memory.
---

# Music domain

Use this skill for every music turn. PostgreSQL `music` is the only operational source of truth. Never derive taste from artist biographies, web research, assistant recommendations, recognition failures, or playback failures.

## Before recommending

Retrieve memory before personalized recommendations:

```bash
hermes-music-memory context --person john --context '{"activity":"general"}' --prompt
```

Apply the returned evidence in this order:

1. Explicit durable dislikes are hard constraints.
2. Matching temporary exclusions apply only to their context and expiry.
3. Explicit likes are positive evidence.
4. Request frequency is inferred affinity, never permission to override an explicit dislike.

Do not expose source IDs or evidence quotations in the conversational reply.

## Classify only when needed

Deterministic commands such as “play X”, “I dislike X”, or “skip” do not need an LLM call. For ambiguous natural language, use bounded Jev classification:

```bash
hermes-music-memory classify 'USER TEXT'
```

The classifier chooses only a fixed kind, scope, and polarity. Entity identity must come from the user's words or the deterministic music resolver. If classification exits non-zero or reports low confidence, do not persist an inference; ask a focused clarification instead.

## Record one interaction

Use a stable, globally unique ingestion key based on the platform and source event, for example `telegram:<chat>:<message>:request`. Repeating the same key is safe and must not create another interaction.

```bash
hermes-music-memory record \
  --person john \
  --kind request \
  --entity-type song \
  --name 'Song title' \
  --artist 'Artist' \
  --platform telegram \
  --session-id 'CHAT_OR_CHANNEL_ID' \
  --message-id 'SOURCE_MESSAGE_ID' \
  --request-id 'PLAYBACK_REQUEST_ID' \
  --ingestion-key 'telegram:CHAT:MESSAGE:request' \
  --quote 'minimal relevant quotation'
```

Record direct durable preferences with `--kind like --preference 1` or `--kind dislike --preference -1`. Record “not tonight” and similar limits as `temporary_exclusion`, `--preference -1`, a non-durable `--scope`, matching JSON `--context`, and `--expires-at`. A skip is `skip`, not a durable dislike.

Corrections and retractions use `--target <interaction-id>`. A correction carries the replacement entity/preference. A retraction carries no entity. Retracting a correction restores the prior evidence; correction chains are resolved by newest effective evidence.

Never store more quotation than needed to audit the classification.

## Playback lifecycle

A request is counted once. Resolution, dispatch, confirmation, and failure are lifecycle states of that same request:

```bash
hermes-music-memory lifecycle \
  --request-ingestion-key 'telegram:CHAT:MESSAGE:request' \
  --status dispatched \
  --ingestion-key 'telegram:CHAT:MESSAGE:lifecycle:dispatched'
```

Allowed statuses: `requested`, `resolved`, `dispatched`, `confirmed`, `failed`.

Only John can confirm that playback was audible. Tool success may justify `dispatched`; it never justifies `confirmed`. Failure is not dislike and must not be written as preference evidence.

## Inspect, correct, and forget

```bash
hermes-music-memory inspect --person john
hermes-music-memory forget --person john --interaction-id ID
hermes-music-memory forget --person john --entity-id ID
```

Show inspection results only to John. Forgetting removes an interaction from retrieval and redacts retained quotation and source identifiers while preserving an ingestion tombstone so historical imports cannot resurrect it.

## Separation rules

- `music.preference_interactions`: John's evidence only.
- `music.playback_lifecycle`: state transitions for one request.
- `music.knowledge_items`: sourced facts and assistant recommendations, never taste evidence.
- `music.artist_names`: existing canonical artist resolver; preserve it.
- Never write controlled test fixtures to John's production person key or schema.

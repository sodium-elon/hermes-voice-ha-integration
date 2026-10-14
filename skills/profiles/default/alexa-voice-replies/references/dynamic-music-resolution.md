# Dynamic Music Resolution for Jarvis → Alexa

Use this when extending `hey_jarvis` music playback so arbitrary artists, genres, albums, playlists, moods, or descriptive song clues reach the configured Echo without turning the voice pipeline into an unrestricted automation agent.

## Routing contract

Preserve this order:

1. Let Home Assistant's native conversation agent try first.
2. Let any enabled live-channel/device resolver try next.
3. If still unmatched and the transcript is a direct `play …` request, send it unchanged to the configured Alexa media player.
4. If the `play …` request is descriptive (lyrics, “that song where…”, “the song from…”, “goes like…”, video/scene clues), resolve it first.
5. On successful Alexa dispatch, return the pipeline's silent-handled sentinel. Do not speak over playback.
6. If resolution is uncertain or unavailable, ask one short clarification and do not send a device command.

Direct requests include artist, genre, album, playlist, station, and “more music.” They must not pay an LLM round trip or depend on hardcoded artist names.

## Safe resolver boundary

The resolver uses the TypeSafe/Jev decision layer, NOT raw `ctx.llm.complete`. Jev (`typesafe-sdk`, model `jev-latest`, key in `~/.hermes/secrets/typesafe.env`) does classification and selection only; it never generates the command text. The shape is:

- `_classify(text)` → Jev `Choice kind` + `Noul` flags (`shuffle`, `research`, `latest`, `studio`, `chained_action`). Returns only `kind` and flags, NOT title/artist names.
- Names are parsed in code (`_strip_verbs`, `by <artist>` regex, possessive latest parsing), not extracted by Jev.
- Ambiguous/lyric-clue targets use "select instead of generate": search the live Apple catalog, present candidates to Jev as a `Choice`, let it pick. This fixed the wrong-song bug (e.g. "Hello From the Other Side" by Jennifer Henderson vs Adele's "Hello").
- Named/exact lookups stay deterministic in code + live catalog (facts, not judgment).

Hard outage contract: if TypeSafe API is unavailable OR credentials are missing, the resolver returns the EXACT string `TypeSafe API (Jev)  is unavailable` (note the double space between `(Jev)` and `is`). The router must return it verbatim; never fall back to a raw command or a clarification guess. All exceptions inside `_classify` are converted to `TypeSafeUnavailable` so a missing SDK/import also honors the contract.

A lyric fragment is untrusted input; do not send it through an unrestricted Sessions agent whose tool surface may include Home Assistant, terminal, email, or other side effects. Final-output validation does not undo tool calls already made during reasoning.

### Known calculation vs. Jev judgment

Keep deterministic facts in code; use Jev only where judgment is genuinely required. Concretely: "latest song/collaboration" recency is a known calculation — sort the live catalog by `releaseDate` desc and take the top deterministically. Do NOT hand "latest" to an open Jev Choice, which misfires on an older-but-famous remix over the actually-newest track.

### Collaboration phrasing

Recognize all of these as a collab request (not just "featuring"): `featuring X`, `feat. X`, `ft. X`, `collaboration with X`, `collab with X`, `collaborating with X`. Use two explicit regex shapes so the captured artist never swallows a trailing "with". The featured artist is extracted and researched against the live catalog (collabs where they appear as a collaborator), ranked by release date. A bare "latest collaboration with X" must route to this path, not fall through to a generic classifier that returns "Which song do you mean?".

### `_strip_verbs` anchoring

Only strip LEADING classifiers ("the", "album/song/playlist"). An unanchored classifier strip corrupts mid-string targets — "latest song featuring Stromae" becomes "latest featuring Stromae" (the "song" was mid-string). Anchor the strip to `^` and keep a separate trailing `\s+(?:playlist|album)$` strip.

Expected model shapes:

```json
{"command":"Play Hello by Adele"}
```

or:

```json
{"clarification":"Which decade was it from?"}
```

Validation before Alexa:

- Parse JSON; reject malformed JSON and non-object values.
- Normalize whitespace only after checking the raw command for separators.
- Reject `;`, CR/LF, `&&`, and `||` to prevent chained custom commands.
- Bound command length (the proven implementation uses 200 characters).
- Require a full case-insensitive match beginning with `play ` and nonempty content.
- If any check fails, return a short clarification and perform no Alexa action.

The custom Alexa command is not a shell, but command chaining is still an unnecessary device-control boundary violation.

## TDD matrix

Write focused tests before implementation for:

1. Direct artist request passes unchanged and never calls the resolver.
2. Direct genre request passes unchanged.
3. Direct album/latest-album request passes unchanged.
4. “Play more music” passes unchanged.
5. A lyric clue invokes the resolver and sends only the resolved `Play TITLE by ARTIST` command.
6. Resolver exception returns a clarification and performs no Alexa action.
7. JSON that is valid but not an object (for example `[]`) is rejected.
8. A chained command such as `Play jazz; turn off all lights` is rejected.
9. Successful playback returns silent-handled rather than a spoken confirmation.

Run the narrow RED test first, implement the smallest behavior, then run the focused group and full suite. If an older test passes only because an API client throws before reaching the intended path, fix the stale test boundary instead of preserving accidental behavior.

## Canonical source vs installed plugin

The editable repository and `~/.hermes/plugins/voice_stack/` can drift independently. Before changing behavior:

1. Compare canonical and installed copies of every relevant plugin file.
2. Treat newer installed behavior as evidence to reconcile, not something to overwrite blindly.
3. Bring proven installed changes back into canonical source before adding new work.
4. Test canonical source.
5. Sync only files that differ.
6. Verify installed hashes/content after syncing.

A particularly subtle failure mode is a caller using newer keyword arguments while canonical helper code still has an older signature. Mock-heavy tests can hide this. Add a direct regression test that verifies important arguments reach the helper boundary.

## Deployment discipline

A plugin code sync does not activate behavior in an already-running gateway process. Restart only with John's explicit authorization. After restart:

1. Verify the service has a new PID and is stable (no restart loop).
2. Reload the Home Assistant Hermes integration entry.
3. Verify the voice WebSocket reports one active connection.
4. Verify required listeners/health endpoints.
5. Inspect the running process environment if behavior depends on systemd variables; validating the unit file alone is insufficient.

If a command is denied, stop the workflow. Do not retry, rephrase, or seek the same state through a different command.

## Real verification

API success is not physical proof. Complete with both:

- Event trace: wake → record → heard exact transcript → route target `alexa_media` with `handled` outcome → empty handled reply.
- John's physical observation that the intended Echo chimed and played the intended content.

For descriptive resolution, first run a no-side-effect resolver probe and inspect the exact generated command. Then, after authorized deployment, use a real phrase such as:

```text
Hey Jarvis … Play that song where they say “hello from the other side” in the lyrics.
```

The expected resolution is `Play Hello by Adele`, but physical playback—not a plausible-looking log—is the completion gate.

## STT mishearing — check the transcript FIRST

When John reports a music request "started something completely different," the resolver may have behaved correctly on a mangled transcript. Before touching resolver code, read the actual heard text:

```text
tail -60 ~/.hermes/logs/voice_events.jsonl
```

and find the `music_resolver` event with `source` = the original utterance. The `heard` text is what the STT captured, not what John said. Foreign/hard names are prime targets: "Stromae" was once transcribed as "Nelly Furtado's latest collaboration with" (truncated before the artist, so no artist to research → generic fallback → wrong collab). The resolver can only act on the text it receives. If the transcript is wrong, the fix is upstream (STT model/prompt/hotwords), not the resolver.

Also confirm HA's own conversation agent returned `no_valid_targets`/`no_intent_match` (an error) — otherwise HA handled it and the music resolver never ran. The routing order is HA → live-channel → music resolver; the resolver only fires on HA error.

Routing pitfall: `no_intent_match` must reach the same Jev music gate as `no_valid_targets`. A real spoken failure produced `"Take two balloons, play the song"` with `code: no_intent_match`; because the gate only lived under `no_valid_targets`, the music path was skipped and the generic agent answered cluelessly. Regression-test both HA error codes.

Low-confidence pitfall: if STT repair returns a clarification, the router must not override it with a legacy raw passthrough just because the utterance looks like a clean `play …` clause. Gate raw passthrough on transcript confidence at/above the repair floor. Below the floor, the heard words are suspect; ask who John meant instead of dispatching garbage like `Play Tuvalu, her latest song featuring Strahan on Apple Music`.

## Artist repair index

A tiny hand-written `artist-vocabulary.txt` is only a seed, not the repair system. For foreign-name STT errors (`Tove Lo` → `Tuvalu`/`two balloons`, `Stromae` → `Strahan`/`Stormlight`), build a local artist-name index and use it to generate real candidates before asking Jev to choose. Good shape:

1. Populate a local DB/table of famous/current artists plus aliases and normalized forms.
2. On John's Palladium setup, prefer the existing local Postgres service over adding DuckDB: connect via socket `/home/john/projects/postgres-hosting.workspace/data/postgres-socket`, DB `palladium`, user `john`, and use `pg_trgm` (`similarity`, `word_similarity`, GIN trigram index) for fuzzy artist matching. Only add DuckDB if Postgres is unavailable or explicitly requested.
3. Query the DB from `_repair_candidates(term)` before appending the small skill vocabulary and Apple catalog candidates. Missing drivers/tables must degrade to `[]`, not kill the voice path.
4. Only pass real DB/catalog/vocabulary candidates to Jev. Jev selects; it does not invent.
5. If candidate confidence is low or `none`, return the clarification. Never raw-passthrough low-confidence music text.

The right fix sequence after a bad voice test is: inspect `heard`/confidence → confirm HA error code → ensure gate ran → repair candidate generation → Jev candidate selection → dry-run command. Do not jump straight to Alexa/Apple Music.

STT model choice matters. If `medium` keeps mangling foreign artist names, upgrade local faster-whisper to `large-v3` before blaming the resolver. Better STT reduces how much fuzzy repair must recover.

## Dry-run lab (verify without firing playback)

To confirm what a spoken phrase WOULD dispatch without touching the Echo, use the `HERMES_VOICE_DRY_RUN` toggle in `_alexa_playback_return` (the single choke point every resolved music command passes through). When set (`1`/`true`/`yes`), it returns speech "Dry run: I would ask Alexa to …" and never calls `_dispatch_alexa_playback`.

- The toggle reads `os.getenv` at call time; flipping it requires setting the var in the gateway environment and a restart. It lives alongside `HERMES_ALEXA_MEDIA_PLAYER` in `/home/john/.config/systemd/user/hermes-gateway.service.d/voice-stack.conf` (kept commented out by default).
- John tests by SPEAKING, not typing. Build the lab as a spoken dry-run mode in the `hey_jarvis` pipeline, not a typing REPL — he explicitly corrected this. The purpose is to prove Jarvis understood his spoken intent before wiring the result to Alexa/Apple Music; dry-run should report the heard text, confidence, route/gate decision, repair result, and exact command that would have been sent.

## Wake cue preservation

Music routing work must not silently regress the historical wake cue. Preserve the configured Alexa announcement chime path and verify the running process actually loaded the intended wake-cue environment. Test the installed cue function once before asking for the final spoken playback probe; dispatch success still requires John to confirm audibility.

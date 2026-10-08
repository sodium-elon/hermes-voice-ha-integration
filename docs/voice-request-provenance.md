# Sportscoach voice request provenance

The voice Sessions API client generates a UUID for each Sportscoach-bound request;
John does not speak or supply an ID. This applies to both Alexa/HA Assist and the
wake-word route because both use `sessions_api.complete(profile="sportscoach")`.

Before the API chat request, the client archives the original text, destination
profile/session, UTC receipt time and `voice:<uuid>` source-message ID in
`$HERMES_HOME/voice/requests/<uuid>.json` (default `~/.hermes/voice/requests`).
The private receipt file uses mode 0600; its directory is created with mode 0700.
If archival fails, the client does not forward an unaudited chat request.

A final JSON metadata block on the current input references that receipt. Static
transport instructions tell Sportscoach to read and compare the archive before
using the generated ID for `coach task-confirm --source-message-id`. The original
speech is preserved in the archive and remains the original input prefix. IDs
and receipt metadata must not be stored as run notes or original user reports.

The ID supplies provenance, **not approval**. Import/correction still requires
genuine user authorization for the exact task scope and the existing confirmation
deadline. No automatic import is triggered by generating a receipt. Repeated
identical speech is two separate requests with two separate IDs; the receipt is
not an idempotency key. Historical tasks are not retroactively approved.

Only Sportscoach requests are decorated; other profile payloads are unchanged.
IDs and paths occur in the current input, not the system prompt, so the additional
system instructions remain byte-stable between voice turns.

Verification:

```sh
python -m pytest tests/test_voice_request_provenance.py tests/test_sessions_api.py tests/test_running_coach_relay.py -q
python -m pytest -q
```

When deploying a narrow patch, copy only `plugins/voice_stack/sessions_api.py`
to the active default-profile voice plugin after comparing the original files.
Do not overwrite unrelated installed voice plugin changes. Activation requires
an explicitly authorized restart of the gateway hosting the voice bridge; a
Sportscoach gateway restart is not required. Verify with a silent read-only
Sessions API request and receipt read-back; never replay a historical import or
perform speaker playback merely as a connectivity check.

Receipts contain private user speech and are stored locally, not committed.
Treat archival retention/cleanup as a separate operation; do not delete receipts
needed to audit pending or completed coach authorization.

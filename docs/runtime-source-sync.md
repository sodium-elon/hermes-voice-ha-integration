# Source-owned voice deployment

The release repository is sodium-elon/hermes-voice-ha-integration. The fork remote is Sodium's release source; origin is upstream. Keep unfinished work in sibling feature worktrees and the primary inner checkout on clean main.

Managed mappings:
- plugins/home_assistant and plugins/voice_stack -> ~/.hermes/plugins
- skills/profiles/default/alexa-voice-replies -> ~/.hermes/skills/smart-home/alexa-voice-replies
- skills/profiles/sportscoach/alexa-voice-replies -> ~/.hermes/profiles/sportscoach/skills/smart-home/alexa-voice-replies
- deploy/alexa/hermes_bridge/lambda_function.py -> the AWS HermesBridge code, deployed separately through the authenticated console/API.

After a reviewed merge, fetch Sodium main and fast-forward the primary checkout. Run the tests before deployment. Run python3 scripts/sync_installed_runtime.py --verify to detect installed drift. For the first reviewed deployment only, use --install --bootstrap; later deployments use --install. Installation requires a clean main checkout, preflights managed files, refuses unexpected installed edits against the prior manifest, backs up replaced files and preserves unmanaged files. The manifest ~/.hermes/.voice-source-sync.json records the source commit and installed hashes. No secrets are copied.

When plugin code changes, activate the default gateway after confirming no current voice task will be interrupted, verify service health and voice WebSocket reconnection, and run --verify again. Refresh Sportscoach after approved skill changes when needed to eliminate cached prompts. Skill installation alone does not prove an already-running session loaded a fresh prompt. A source-matched file is not proof of physical microphone capture; use a separately requested harmless pending-question test for behavior changes.

The source helper does not update AWS, restart services, merge PRs or manage Hermes core. Verify AWS code against the versioned relay file after an authorized Lambda deployment. Preserve unrelated core and feature work; do not replace the live plugin from a dirty feature checkout or an unspecified packaged installer.

Sportscoach's run/coaching skills remain owned by sodium-elon/alpharunner, using scripts/install-sportscoach-skills.py --install and --verify from its clean main. Hermes is responsible for reconciling installed guidance into that source and verifying the managed snapshots after a merge; memory changes are not a substitute.

Existing voice deadlines, real user authorization and one-shot answer rules remain unchanged.

## Sportscoach role and learning ownership

AlphaRunner owns coaching doctrine (`skills/running-coaching/SKILL.md`) and the profile capability contract `deploy/sportscoach-profile-policy.json`; this voice repository owns only transport and its profile-specific Alexa guidance. The Sessions API relay selects the target profile endpoint and sends session identity/title plus chat input/system text; it does not grant additional tools or override the target profile's toolsets. Existing receipt provenance and dated-shoe resolution are not engineering-maintenance authorization. Keep domain procedures in the owning coaching skill, not relay prompts.

Ordinary coaching, missing data and lost spoken answers must not launch coding, configuration changes, ingestion repair or scheduled work. Full listening diagnostics require an explicit engineering request. A protected coaching-skill correction refusal preserves the proposed learning and evidence, marks it not installed, and hands it to the owning AlphaRunner source for a reviewed PR; shell writes, another profile or alternate-tool bypasses are not allowed. Voice-only corrections follow this repository's reviewed source workflow. Neither handoff authorizes implementation or deployment: PR author sodium-elon, reviewer johnroma, with John's GitHub approval required before merge/deploy. The default/main profile retains its normal Alexa capabilities.

## Completing and retiring feature work

After a verified merge and main deployment, remove the completed feature checkout.
For unfinished work, compare every changed and untracked file against current main,
complete and merge useful independent changes, then explicitly confirm removal of
obsolete checkouts after making a verified recoverable archive. Do not leave a
superseded prototype described as pending production repair.

The native-alexa-delayed-dialogue experiment is retired as a proposed replacement
for the current confirmation fix: the existing HermesBridge reprompt route passed
the physical Sportscoach yes/no test. The experiment needs a new full-envelope
HA endpoint and exact Amazon-device mapping; its controller supports only music
confirmation and its legacy Assist path predates current Sportscoach context and
admission safeguards. It must not replace the working route. Reusable request-gate,
recording, provenance, forwarding, dispatch tests and operational guidance have
been incorporated separately into main. Full-envelope identity could be considered
in a separately scoped future change with Sportscoach support and physical
verification; the archived experiment is not a deployable release.

Music preferences belong in sodium-elon/music-preferences. The old voice-repository
feature checkout was superseded by that repository's merged PR 1; do not reintroduce
the database, CLI and DJ feature into the voice integration.

## Alexa question capture and binary confirmations

HermesBridge uses Dialog.ElicitSlot for ordinary launches, empty questions and
open follow-up prompts because HermesIntent.query is AMAZON.SearchQuery. Its
configured dialog model is required; a plain reprompt only recognizes intent
samples with carrier phrases. A pending confirmation is distinguished by the
HA reprompt "Please answer yes or no." and retains native YesIntent/NoIntent
recognition without query-slot elicitation. Validate both long open questions
and specialist yes/no context when changing either mode. Log only request type,
intent and whether the query exists, plus Alexa's session-ending reason and bounded
validation error detail; never log raw envelopes or access tokens. SessionEndedRequest
is handled locally with an empty response; it has no speech or HA action.

Explicit conversation-ending judgment, its source-owned phrase corpus and relay-first
deployment order are described in [conversation-ending.md](conversation-ending.md).

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

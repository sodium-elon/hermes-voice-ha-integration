# Ending an Alexa conversation

Hermes judges a likely closing utterance with one Jev Choice over close, continue
and uncertain. The source-owned examples are in
plugins/voice_stack/conversation_end_examples.json: 60 closing examples, 36
continuation examples and 16 ambiguous acknowledgments, plus 20 held-out evaluation
cases that are not sent in the classifier prompt. Examples are synthetic, not
recorded private speech.

A cheap hint filter selects potential closing speech; it never closes on its own.
Only a valid close decision with probability at least 0.9 closes. Jev sees the
whole utterance and counterexamples: Thanks, but another question continues;
Stop the music remains a music command; a quoted goodbye remains a question.
Bare okay, done or stop are ambiguous to this classifier. Native Alexa Stop and
Cancel intents retain their existing terminal behavior.

Native yes/no is handled first. Polite Yes, thanks and No, thank you go once to
the saved specialist session if it has an armed question. An unarmed polite
farewell can be judged normally. Closing never grants approval, executes a task
or cancels a domain operation. It abandons the pending voice question so a later
yes cannot approve that abandoned exchange. Existing 30-second confirmation,
180-second in-flight and 60-second Jarvis deadlines are unchanged.

The classifier has one worker and one admission slot. Its SDK timeout is 0.6
seconds without retries; its awaited wall-clock budget is 0.75 seconds. That time
comes from the existing Alexa response budget, rather than extending it.
Missing credentials, saturation, timeout, failure, malformed output and low
confidence keep the dialogue open. Late judgments cannot act on another turn.
Logs contain only label, probability and outcome, never the utterance.

The gateway returns the exact internal speech signal
__hermes_conversation_end_v1__ for a confident closing decision. This crosses the
existing HA speech template without an HA configuration change. HermesBridge
recognizes only an exact gateway PlainText response signal, replaces it with
Goodbye., sets shouldEndSession=true, and removes reprompts and Dialog directives.
The trusted gateway response boundary permits this token only from the close
branch; model-generated exact echoes are escaped as speech data, including slow
agent completions. User query text, quoted signals and ordinary agent prose do not
close by this transport rule. Ordinary question capture and binary confirmation modes remain
as documented in runtime-source-sync.md.

## Verification and deployment

Run pytest and git diff --check. The offline tests cover transport contracts,
polite binary answers, malformed judgments, deadlines, saturation and late-result
safety. They do not substitute for real Jev semantic evaluation.

Run scripts/evaluate_conversation_end.py --output PATH with the established
Hermes interpreter to evaluate all synthetic corpus and held-out cases without
agent dispatch or audio. Report false closes separately from conservative missed
closes and service failures; do not describe examples embedded in the prompt as
held-out accuracy. The evaluator uses the live credential without printing it.

Merge reviewed changes into release main. Deploy and hash-verify HermesBridge
BEFORE installing the gateway change, so the internal signal cannot be spoken.
Then use scripts/sync_installed_runtime.py --install from clean main, activate
the default gateway when no turn will be interrupted, and verify managed files
and HA reconnection. A physical user-controlled check should cover a long open
question, Thanks, that is all, Thanks but another question and a pending yes/no.
An accepted HA delivery or simulator input is not physical microphone evidence.

## Initial live evaluation, 15 October 2026

The full synthetic pipeline evaluation produced 132/132 expected close/keep-open
outcomes: 112 catalogued examples plus 20 held-out cases. Jev judged 114 inputs;
18 inputs without closing hints stayed open without a network call. There were
zero false closes and zero missed expected closes in this run. Judged-call median
was 0.2175 seconds and maximum 0.414 seconds, below the 0.75-second awaited budget.
This is one measured example set, not a guarantee for arbitrary future speech.
The prompt examples and held-out cases are reported separately above.

## Delayed general answers and the listening window

A general answer that outlives Alexa's initial request budget is returned through
the same native skill on the original asking Echo. Cache the completed answer,
re-enter the skill, speak it from Launch, then use the ordinary query reprompt.
Consume that cached answer once; it is never a binary confirmation and cannot
authorize a task. This lets Alexa begin listening after the real answer rather
than timing out while a separate notification is being spoken.

The existing 180-second stale-result cap remains. Superseded or expired general
answers cannot reopen the microphone. If the requesting Echo is unknown or native
re-entry fails, deliver the original answer by the existing notification fallback
without claiming a fresh microphone window. Specialist confirmation and
declarative task-completion policies remain unchanged.

Re-entry network I/O runs outside the general-result state lock. Cleanup on an
immediate failure checks that the same pending object is still current, so a
concurrent Launch or newer request cannot cause duplicate speech or erase newer
state. HA accepting the launch service is not proof that a native Launch arrived.
If acceptance is followed by no Launch, the answer stays cached until its existing
expiry; a manual skill opening can retrieve it. There is no asynchronous retry
watchdog that could race with a late Launch and speak twice.

## Native reopening validation, 15 October 2026

A physical delayed-answer test exposed an Alexa INVALID_RESPONSE after speech:
"The following directives are not supported: Dialog.ElicitSlot". A controlled
skill-ID shortcut reproduced the same error immediately. The existing binary
confirmation route does not return that directive and remains on its physically
verified shortcut.

General delayed answers now reopen using Alexa Media Player's custom command
"open Hermes Assistant", a fixed invocation rather than user/model content.
The controlled native launch returned READY with query elicitation and ended
normally with EXCEEDED_MAX_REPROMPTS after no reply, instead of INVALID_RESPONSE.
The primary Alexa Media Player 5.16.0 source supports custom through run_custom:
https://github.com/alandtse/alexa_media_player/blob/v5.16.0/custom_components/alexa_media/media_player.py#L1843

This distinguishes an accepted native listening response from the former error.
It does not prove microphone recognition or an audible Goodbye; those require a
separate user-controlled Echo test. No broad removal of query elicitation, model
change, additional approval or confirmation-deadline extension is needed.

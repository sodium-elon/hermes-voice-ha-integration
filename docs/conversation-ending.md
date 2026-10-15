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

# Native Alexa bounded confirmation — existing Hermes Assistant route

## Sportscoach extension

The same route now supports Sportscoach as well as DJ. Coach's terminal
"confirm October third is the date you want imported." is a bounded yes/no
request even without a question mark. Supported patterns are conservative:
standard yes/no starters or coach-only "confirm [that] X is/are Y."; open-ended
questions, unrelated profiles and replies over 600 characters do not arm it.
This is a wording heuristic, not semantic validation of arbitrary prose.

- Slow replies reuse the existing skill relaunch and Launch reprompt, never a
  one-way notification for a recognized confirmation.
- Fast replies return the question plus native reprompt directly, with no
  notification or skill relaunch.
- One yes/no is consumed before dispatch to the saved specialist. The original
  request, question and answer are forwarded together, with a Coach/DJ label.
  The answer's result cannot arm a further confirmation.
- Answers expire at `now >= deadline`: 30 seconds from arming/response
  construction, **not measured speech completion**. A new request cancels the
  old record; stale generations cannot overwrite it. Jarvis remains separate.

Repo tests use fake agent/device boundaries. They do not import a real run or
prove physical Echo listening. No new skill, Lambda route, HA intent names or
Jarvis microphone changes are required by this extension. Existing HA/Amazon
configuration below must already be enabled for the native path to function.
Deployment/restarts, current live HA wiring and physical testing are separate
release gates; commit/push alone changes none of them.

## Current-request isolation and same-route answers

A new Alexa/HA specialist request now uses a fresh Sessions API ID/title instead
of reopening the shared `alexa_hermes` agent history. The HA conversation ID is
still echoed unchanged on the WebSocket response. The bounded pending record
pins the new agent session/title, original request and original Echo-target
future; a yes/no reuses those exact values. Nothing imports, corrects or resumes
an older run merely because it exists in history. The archived voice receipt
provides original text and receipt time for the main coach's date anchoring.

Sportscoach's "did you mean the Saucony Kinvara 16?" is a supported binary
question, alongside the existing DJ prompts and coach confirmation imperatives.
Open-ended questions remain excluded. Fast replies propagate the native reprompt;
slow replies use the SAME existing Hermes Assistant skill on the captured Echo,
not a Jarvis microphone or a one-way notification question. Both reserved HA
Yes/No markers and exact plain yes/no from HermesIntent consume the currently
armed answer once, including when HA uses a different conversation ID.
Unprompted, expired, superseded and repeated answers do not start an agent.

The answer window remains 30 seconds from arming, not measured speech completion;
the Jarvis 60-second wake window is unchanged. HA intent wiring alone does not
prove Amazon Yes/No model support or physical capture. The relay currently has no
authenticated Amazon device identity; retained last_called targeting is household
best-effort continuity, not proof of which person/Echo supplied an answer.

`launch_skill` now uses the enabled voice plugin's own HA credential/config reader
and a bounded authenticated REST call; it does not import a disabled sibling
plugin. Service acceptance is still not proof that Echo captured a spoken answer.

Regression commands:

```sh
python -m pytest tests/test_coach_alexa_dialogue.py tests/test_alexa_launch_transport.py tests/test_voice_request_provenance.py -q
python -m pytest -q
```

## Original route deployment reference

## Goal

When the DJ (music profile) finishes a *slow* Alexa Assist turn with a yes/no
question ("Want me to play it?"), that question must be re-opened as a **native
Alexa dialogue** so the Echo listens for one "yes"/"no" — instead of being spoken
as a one-way `notify.alexa_media` message that never captures an answer.

The Jarvis wake pipeline and its absolute 60s follow-up deadline are **unchanged**.

### What is verified (in this branch)

| Layer | File | Status |
|---|---|---|
| Bounded pending record | `plugins/voice_stack/alexa_confirmation.py` | tested |
| Yes/no eligibility heuristic | `_CONFIRM_RE` in same file | tested |
| Skill relaunch helper | `launch_skill()` → `media_player.play_media(media_content_type=skill)` | tested (mocked) |
| Assist LAUNCH/YES/NO routing | `plugins/voice_stack/__init__.py::_handle_assist_query_with_llm` | tested |
| Slow-path begin/deliver wiring | `_schedule_slow_delivery` / `_deliver_when_done_with_target` | tested |
| Reprompt propagation | `custom_components/hermes/conversation.py::async_process` | tested (static) |

`pytest -q` → **436 passed** (409 baseline + 27 confirmation/wiring tests).

The reprompt mechanism is confirmed against upstream HA core: `IntentResponse.async_set_speech(speech, extra_data=...)` stores `{"extra_data": ...}` under `speech["plain"]`, and `intent_script` skips reprompt when the rendered text is falsy (`if text_reprompt:`). So an empty reprompt ends the Alexa session exactly as a plain reply does today.

## Deployment gates — keep these separate from code verification

1. **Home Assistant wiring.** The query reprompt, skill Launch marker and
   Yes/No handlers have been applied on John's HA instance and `ha core check`
   passed. The YAML below is the versioned configuration contract, not a claim
   that a new gateway patch has been activated. Recheck wiring before release.

2. **Amazon interaction model.** `AMAZON.YesIntent` / `AMAZON.NoIntent` must
   exist in the skill model in the Alexa developer console. HA YAML alone does
   not add them. This could not be verified (console login required).

3. **Physical Echo test.** A service ACK does not prove the Echo re-opened its
   microphone. This must be verified by hand after deploy.

## `configuration.yaml` contract (apply only after approval)

```yaml
intent_script:
  HermesIntent:
    async_action: false
    action:
      - action: conversation.process
        data:
          agent_id: conversation.hermes
          text: "{{ query }}"
          language: en
          conversation_id: alexa_hermes
        response_variable: hermes_result
      - stop: "Return the Hermes conversation response"
        response_variable: hermes_result
    speech:
      type: plain
      text: "{{ action_response.response.speech.plain.speech }}"
    reprompt:
      type: plain
      text: "{{ action_response.response.speech.plain.extra_data.reprompt | default('', true) }}"

  amzn1.ask.skill.089b605e-8c45-4e3a-b5d2-9fca1ad7ed67:
    async_action: false
    action:
      - action: conversation.process
        data:
          agent_id: conversation.hermes
          text: "__hermes_alexa_launch__"
          language: en
          conversation_id: alexa_hermes
        response_variable: hermes_result
      - stop: "Return the Hermes launch response"
        response_variable: hermes_result
    speech:
      type: plain
      text: "{{ action_response.response.speech.plain.speech }}"
    reprompt:
      type: plain
      text: "{{ action_response.response.speech.plain.extra_data.reprompt | default('', true) }}"

  AMAZON.YesIntent:
    async_action: false
    action:
      - action: conversation.process
        data:
          agent_id: conversation.hermes
          text: "__hermes_alexa_yes__"
          language: en
          conversation_id: alexa_hermes
        response_variable: hermes_result
      - stop: "Return the Hermes confirmation response"
        response_variable: hermes_result
    speech:
      type: plain
      text: "{{ action_response.response.speech.plain.speech }}"

  AMAZON.NoIntent:
    async_action: false
    action:
      - action: conversation.process
        data:
          agent_id: conversation.hermes
          text: "__hermes_alexa_no__"
          language: en
          conversation_id: alexa_hermes
        response_variable: hermes_result
      - stop: "Return the Hermes confirmation response"
        response_variable: hermes_result
    speech:
      type: plain
      text: "{{ action_response.response.speech.plain.speech }}"
```

Both the query (`HermesIntent`) and Launch handlers must propagate the returned
`reprompt` field. A pending confirmation returns the yes/no reprompt. A plain
launch retains the ordinary ready greeting and reprompt; a non-confirmation
query returns an empty reprompt and ends its session. Yes/No handlers have no
reprompt, so consuming an answer closes native listening rather than re-arming it.
`intent_script` skips a falsy reprompt string.

## Trust model / limitation (accepted for this household)

The existing `conversation.hermes` route carries **no Amazon device/user/session
identity**. It therefore cannot prove which Echo — or which person — issued the
relaunch or the yes/no. `last_called` is best-effort *delivery targeting*, not
authorization. This matches the existing shared-household trust; stronger
per-Echo isolation (full-envelope forwarding) is deliberately out of scope here.

## Rollback

For this coach extension, revert its commit and redeploy the prior plugin files;
keep the existing music confirmation route. If enabling the prospective HA wiring
as part of a separate release, first back up its current intent blocks and restore
those exact blocks on rollback (including `HermesIntent`'s reprompt). Never enable
HA marker forwarding before the matching gateway handler is loaded and verified.
The steps below describe rollback of the *original* route, not just this extension.

1. Revert `plugins/voice_stack/__init__.py` and delete
   `plugins/voice_stack/alexa_confirmation.py`.
2. Restore the prior static Launch intent block and remove the added
   Yes/No intent blocks from `configuration.yaml`.
3. Reload the Alexa/HA integrations.

## Physical verification checklist (user-controlled)

- [ ] `AMAZON.YesIntent` / `AMAZON.NoIntent` present in the Alexa skill model.
- [ ] Echo speaks the DJ question and **stays listening** (reprompt).
- [ ] Saying "yes" plays the song; saying "no" is declined; no answer expires.
- [ ] A new request cancels the pending question.
- [ ] Jarvis 60s deadline behavior is unchanged.
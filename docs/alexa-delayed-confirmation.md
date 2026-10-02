# Native Alexa delayed-confirmation (existing-route) — deployment & rollback

Branch: `fix/alexa-existing-route-confirmation`
Base: `main` @ `e570a1c`

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

## What is NOT yet done (external gates — not claimable as working)

1. **Home Assistant `configuration.yaml` (prospective, not applied).**
   The skill Launch intent is currently *static* speech. It must instead route
   through `conversation.hermes` with the reserved LAUNCH marker and a
   conditional reprompt; Yes/No intents must be added. See YAML below.

2. **Amazon interaction model.** `AMAZON.YesIntent` / `AMAZON.NoIntent` must
   exist in the skill model in the Alexa developer console. HA YAML alone does
   not add them. This could not be verified (console login required).

3. **Physical Echo test.** A service ACK does not prove the Echo re-opened its
   microphone. This must be verified by hand after deploy.

## Prospective `configuration.yaml` changes (apply only after approval)

```yaml
intent_script:
  HermesIntent:
    # unchanged

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

The Launch intent's `reprompt:` is a template that reads the `reprompt` field the
custom component now propagates (empty when there is no pending question, so
Alexa ends the session exactly as it does today for a plain "open Hermes").
Because `intent_script` skips a falsy reprompt string, no extra conditional is
needed.

## Trust model / limitation (accepted for this household)

The existing `conversation.hermes` route carries **no Amazon device/user/session
identity**. It therefore cannot prove which Echo — or which person — issued the
relaunch or the yes/no. `last_called` is best-effort *delivery targeting*, not
authorization. This matches the existing shared-household trust; stronger
per-Echo isolation (full-envelope forwarding) is deliberately out of scope here.

## Rollback

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
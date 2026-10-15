# -*- coding: utf-8 -*-
import os
import json
import logging
import urllib3

_debug = os.environ.get('DEBUG', '').lower() in ('1', 'y', 'yes', 'true', 'on')

_logger = logging.getLogger('HomeAssistant-Intents')
_logger.setLevel(logging.DEBUG if _debug else logging.INFO)

# After these, keep the mic open so the conversation can continue.
_CONVERSATION_INTENTS = (
    'HermesIntent',
    'AMAZON.HelpIntent',
    'AMAZON.FallbackIntent',
)

# These must always be allowed to close the session.
_TERMINAL_INTENTS = (
    'AMAZON.StopIntent',
    'AMAZON.CancelIntent',
    'AMAZON.NavigateHomeIntent',
)

_ELICIT_PROMPT = 'What would you like to know?'
_BINARY_REPROMPT = 'Please answer yes or no.'
_END_SIGNAL = '__hermes_conversation_end_v1__'


def _query_directive():
    return {
        'type': 'Dialog.ElicitSlot',
        'slotToElicit': 'query',
        'updatedIntent': {
            'name': 'HermesIntent',
            'confirmationStatus': 'NONE',
            'slots': {'query': {'name': 'query', 'confirmationStatus': 'NONE'}},
        },
    }


def _speak_and_listen(text):
    """Capture an open question using the configured SearchQuery dialog."""
    return {
        'version': '1.0',
        'sessionAttributes': {},
        'response': {
            'outputSpeech': {'type': 'PlainText', 'text': text},
            'reprompt': {'outputSpeech': {'type': 'PlainText', 'text': text}},
            'shouldEndSession': False,
            'directives': [_query_directive()],
        },
    }


def _keep_listening(payload):
    """Capture open questions; preserve native yes/no for pending confirmations."""
    if not isinstance(payload, dict):
        return payload

    body = payload.get('body') if isinstance(payload.get('body'), dict) else payload
    response = body.get('response')
    if not isinstance(response, dict):
        return payload

    if not response.get('reprompt'):
        response['reprompt'] = {
            'outputSpeech': {'type': 'PlainText', 'text': _ELICIT_PROMPT},
        }
    prompt = response['reprompt'].get('outputSpeech', {}).get('text', '')
    if prompt != _BINARY_REPROMPT:
        directives = response.setdefault('directives', [])
        if not any(d.get('type', '').startswith('Dialog.') for d in directives):
            directives.append(_query_directive())
    response['shouldEndSession'] = False
    return payload


def _consume_end_signal(payload):
    """An exact gateway speech signal closes natively; never inspect user words."""
    if not isinstance(payload, dict):
        return False
    body = payload.get('body') if isinstance(payload.get('body'), dict) else payload
    response = body.get('response')
    if not isinstance(response, dict):
        return False
    speech = response.get('outputSpeech') or {}
    if speech.get('type') != 'PlainText' or speech.get('text') != _END_SIGNAL:
        return False
    response['outputSpeech'] = {'type': 'PlainText', 'text': 'Goodbye.'}
    response['shouldEndSession'] = True
    response.pop('reprompt', None)
    directives = [d for d in response.get('directives', [])
                  if not d.get('type', '').startswith('Dialog.')]
    if directives:
        response['directives'] = directives
    else:
        response.pop('directives', None)
    return True


def lambda_handler(event, context):
    """Handle incoming Alexa directive."""

    _logger.debug('Event: %s', event)

    base_url = os.environ.get('BASE_URL')
    assert base_url is not None, 'Please set BASE_URL environment variable'

    try:
        token = event.get('session', {}).get('user', {}).get('accessToken')
    except AttributeError:
        token = None

    if token is None and _debug:
        token = os.environ.get('LONG_LIVED_ACCESS_TOKEN')

    assert token, 'Could not get access token'

    request = event.get('request') or {}
    request_type = request.get('type')
    intent = request.get('intent') or {}
    intent_name = intent.get('name')
    _logger.info('Alexa request type=%s intent=%s query_present=%s',
                 request_type, intent_name,
                 bool((intent.get('slots') or {}).get('query', {}).get('value')))

    # An empty general query needs slot capture; pending binary questions use
    # the HA reprompt and bypass this path.
    if request_type == 'IntentRequest' and intent_name == 'HermesIntent':
        slot = (intent.get('slots') or {}).get('query') or {}
        if not slot.get('value'):
            _logger.debug('Empty query slot, eliciting')
            return _speak_and_listen(_ELICIT_PROMPT)

    verify_ssl = os.environ.get('NOT_VERIFY_SSL', '').lower() not in ('1', 'y', 'yes', 'true', 'on')

    http = urllib3.PoolManager(
        cert_reqs='CERT_REQUIRED' if verify_ssl else 'CERT_NONE',
        timeout=urllib3.Timeout(connect=2.0, read=10.0)
    )

    response = http.request(
        'POST',
        '{}/api/alexa'.format(base_url),
        headers={
            'Authorization': 'Bearer {}'.format(token),
            'Content-Type': 'application/json',
        },
        body=json.dumps(event).encode('utf-8'),
    )
    if response.status >= 400:
        return {
            'event': {
                'payload': {
                    'type': 'INVALID_AUTHORIZATION_CREDENTIAL'
                    if response.status in (401, 403) else 'INTERNAL_ERROR',
                    'message': response.data.decode("utf-8"),
                }
            }
        }

    payload = json.loads(response.data.decode('utf-8'))

    if _consume_end_signal(payload):
        return payload

    keep_open = (
        request_type == 'LaunchRequest'
        or (request_type == 'IntentRequest'
            and intent_name in _CONVERSATION_INTENTS
            and intent_name not in _TERMINAL_INTENTS)
    )
    if keep_open:
        payload = _keep_listening(payload)

    return payload

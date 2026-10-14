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


def _speak_and_listen(text):
    """Speak a prompt and keep native intent recognition open."""
    return {
        'version': '1.0',
        'sessionAttributes': {},
        'response': {
            'outputSpeech': {'type': 'PlainText', 'text': text},
            'reprompt': {'outputSpeech': {'type': 'PlainText', 'text': text}},
            'shouldEndSession': False,
        },
    }


def _keep_listening(payload):
    """Preserve HA's native reprompt; do not force query-slot dialog."""
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
    response['shouldEndSession'] = False
    return payload


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

    # An empty query needs a prompt, with ordinary intent recognition enabled.
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

    keep_open = (
        request_type == 'LaunchRequest'
        or (request_type == 'IntentRequest'
            and intent_name in _CONVERSATION_INTENTS
            and intent_name not in _TERMINAL_INTENTS)
    )
    if keep_open:
        payload = _keep_listening(payload)

    return payload

import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

fake_http = types.SimpleNamespace()
stub_http = types.SimpleNamespace(PoolManager=lambda **kwargs: fake_http, Timeout=lambda **kwargs: kwargs)
spec = importlib.util.spec_from_file_location('bridge', Path(__file__).resolve().parents[1] / 'deploy/alexa/hermes_bridge/lambda_function.py')
bridge = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'urllib3': stub_http}):
    spec.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def call(self, request, response):
        calls = []
        fake_http.request = lambda *args, **kwargs: (calls.append((args, kwargs)) or types.SimpleNamespace(status=200, data=json.dumps(response).encode()))
        event = {'session': {'user': {'accessToken': 'test-token'}}, 'request': request}
        with patch.dict(os.environ, {'BASE_URL': 'https://test.invalid'}):
            result = bridge.lambda_handler(event, None)
        return result, calls, event

    def test_pending_launch_preserves_binary_reprompt_without_slot_directive(self):
        response = {'version': '1.0', 'sessionAttributes': {}, 'response': {'outputSpeech': {'type': 'PlainText', 'text': 'Did you mean this shoe, yes or no?'}, 'reprompt': {'outputSpeech': {'type': 'PlainText', 'text': 'Please answer yes or no.'}}, 'shouldEndSession': False}}
        result, calls, event = self.call({'type': 'LaunchRequest'}, response)
        self.assertEqual(result, response)
        self.assertNotIn('directives', result['response'])
        self.assertEqual(json.loads(calls[0][1]['body']), event)

    def test_yes_and_no_forward_unchanged_and_can_close(self):
        for name in ('AMAZON.YesIntent', 'AMAZON.NoIntent'):
            with self.subTest(name=name):
                response = {'version': '1.0', 'response': {'outputSpeech': {'type': 'PlainText', 'text': 'Answer received'}, 'shouldEndSession': True}}
                result, calls, event = self.call({'type': 'IntentRequest', 'intent': {'name': name}}, response)
                self.assertEqual(result, response)
                self.assertEqual(json.loads(calls[0][1]['body']), event)

    def test_stop_cancel_and_home_close(self):
        for name in bridge._TERMINAL_INTENTS:
            result, _, _ = self.call({'type': 'IntentRequest', 'intent': {'name': name}}, {'response': {'shouldEndSession': True}})
            self.assertTrue(result['response']['shouldEndSession'])
            self.assertNotIn('reprompt', result['response'])

    def test_empty_query_captures_open_question_without_network(self):
        result, calls, _ = self.call({'type': 'IntentRequest', 'intent': {'name': 'HermesIntent', 'slots': {}}}, {})
        self.assertEqual(calls, [])
        self.assertFalse(result['response']['shouldEndSession'])
        self.assertEqual(result['response']['directives'], [bridge._query_directive()])
        self.assertEqual(result['response']['reprompt']['outputSpeech']['text'], bridge._ELICIT_PROMPT)

    def test_general_reply_captures_next_open_question(self):
        result, _, _ = self.call({'type': 'IntentRequest', 'intent': {'name': 'HermesIntent', 'slots': {'query': {'value': 'hello'}}}}, {'response': {'outputSpeech': {'type': 'PlainText', 'text': 'Hello'}, 'shouldEndSession': True}})
        self.assertFalse(result['response']['shouldEndSession'])
        self.assertEqual(result['response']['reprompt']['outputSpeech']['text'], bridge._ELICIT_PROMPT)
        self.assertEqual(result['response']['directives'], [bridge._query_directive()])

    def test_wrapped_payload_preserves_existing_directives(self):
        result = bridge._keep_listening({'body': {'response': {'directives': [{'type': 'Display.RenderTemplate'}], 'reprompt': {'outputSpeech': {'type': 'PlainText', 'text': 'Please answer yes or no.'}}}}})
        self.assertEqual(result['body']['response']['directives'], [{'type': 'Display.RenderTemplate'}])
        self.assertEqual(result['body']['response']['reprompt']['outputSpeech']['text'], 'Please answer yes or no.')

    def test_ordinary_launch_captures_open_question(self):
        result, _, _ = self.call({'type': 'LaunchRequest'}, {
            'response': {'outputSpeech': {'type': 'PlainText', 'text': 'Ready'},
                         'reprompt': {'outputSpeech': {'type': 'PlainText', 'text': 'Ready'}}}})
        self.assertEqual(result['response']['directives'], [bridge._query_directive()])

    def test_fast_binary_question_preserves_yes_no_recognition(self):
        result, _, _ = self.call({'type': 'IntentRequest', 'intent': {
            'name': 'HermesIntent', 'slots': {'query': {'value': 'play music'}}}}, {
            'response': {'outputSpeech': {'type': 'PlainText', 'text': 'Would you like jazz?'},
                         'reprompt': {'outputSpeech': {'type': 'PlainText', 'text': bridge._BINARY_REPROMPT}}}})
        self.assertNotIn('directives', result['response'])
        self.assertFalse(result['response']['shouldEndSession'])

    def test_fallback_captures_repeated_question(self):
        result, _, _ = self.call({'type': 'IntentRequest', 'intent': {'name': 'AMAZON.FallbackIntent'}},
                                {'response': {'outputSpeech': {'type': 'PlainText', 'text': 'Please repeat.'}}})
        self.assertEqual(result['response']['directives'], [bridge._query_directive()])

    def test_open_capture_preserves_display_and_avoids_duplicate_dialog(self):
        payload = {'response': {'directives': [{'type': 'Display.RenderTemplate'}]}}
        result = bridge._keep_listening(payload)
        bridge._keep_listening(result)
        self.assertEqual(result['response']['directives'],
                         [{'type': 'Display.RenderTemplate'}, bridge._query_directive()])


    def test_gateway_end_signal_closes_without_query_capture(self):
        result, _, _ = self.call({'type': 'IntentRequest', 'intent': {
            'name': 'HermesIntent', 'slots': {'query': {'value': 'thanks'}}}}, {
            'body': {'response': {
                'outputSpeech': {'type': 'PlainText', 'text': bridge._END_SIGNAL},
                'reprompt': {'outputSpeech': {'type': 'PlainText', 'text': 'Ready'}},
                'directives': [bridge._query_directive()],
                'shouldEndSession': False}}})
        response = result['body']['response']
        self.assertEqual(response['outputSpeech']['text'], 'Goodbye.')
        self.assertTrue(response['shouldEndSession'])
        self.assertNotIn('reprompt', response)
        self.assertNotIn('directives', response)

    def test_user_text_or_quoted_signal_cannot_close(self):
        for text in ('You said ' + bridge._END_SIGNAL, 'Goodbye.', 'Thanks!'):
            result, _, _ = self.call({'type': 'IntentRequest', 'intent': {
                'name': 'HermesIntent', 'slots': {'query': {'value': bridge._END_SIGNAL}}}},
                {'response': {'outputSpeech': {'type': 'PlainText', 'text': text}}})
            self.assertFalse(result['response']['shouldEndSession'])

    def test_missing_token_does_not_send_request(self):
        with patch.dict(os.environ, {'BASE_URL': 'https://test.invalid'}), patch.object(bridge, '_debug', False):
            with self.assertRaisesRegex(AssertionError, 'access token'):
                bridge.lambda_handler({'request': {'type': 'LaunchRequest'}}, None)


if __name__ == '__main__':
    unittest.main()

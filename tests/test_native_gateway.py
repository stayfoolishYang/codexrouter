import json
import hashlib
import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_gateway import History, EventMapper, Server, native_args, prepare_request, open_with_queue_fallback
from native_launcher import desktop_environment

ROUTES = {'deepseek-flash': {'base_url': 'https://api.deepseek.com', 'key_env': 'DEEPSEEK_API_KEY', 'efforts': ['medium'], 'queue_fallback_model': 'deepseek-v4-pro', 'queue_fallback_seconds': 20}}

class NativeGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = History(Path(self.tmp.name) / 'calls.json')
        self.mapper = EventMapper(ROUTES, self.history)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name='native_delegate_task', args=None):
        return {'type': 'function_call', 'id': 'item1', 'call_id': 'call1', 'name': name,
                'arguments': json.dumps(args or {'task_name': 'file_probe', 'task_text': 'Read 中文/path.txt', 'model': 'deepseek-flash', 'reasoning_effort': 'medium'})}

    def test_streamed_arguments_are_mapped_only_when_complete(self):
        item = self.call()
        self.assertEqual(self.mapper.event({'type': 'response.output_item.added', 'item': {**item, 'arguments': ''}}), [])
        self.assertEqual(self.mapper.event({'type': 'response.function_call_arguments.delta', 'item_id': 'item1', 'delta': '{"task_text":'}), [])
        events = self.mapper.event({'type': 'response.output_item.done', 'item': item})
        self.assertEqual(len(events), 2)
        mapped = events[-1]['item']
        self.assertEqual(mapped['name'], 'spawn_agent')
        self.assertEqual(mapped['namespace'], 'collaboration')
        self.assertEqual(mapped['encrypted_function_args'], [])
        self.assertEqual(json.loads(mapped['arguments'])['message'], 'Read 中文/path.txt')

    def test_history_survives_restart_without_storing_task(self):
        item = self.mapper.item(self.call())
        history = History(self.history.path)
        restored = history.restore(item)
        self.assertEqual(restored['name'], 'native_delegate_task')
        self.assertEqual(json.loads(restored['arguments'])['task_text'], 'Read 中文/path.txt')
        self.assertNotIn('Read', self.history.path.read_text())
        self.assertNotIn('namespace', restored)

    def test_other_native_calls_and_ciphertext_unchanged(self):
        item = {**self.call(), 'namespace': 'collaboration', 'name': 'spawn_agent', 'encrypted_function_args': ['opaque']}
        self.assertEqual(self.mapper.item(item), item)
        self.assertEqual(self.history.restore(item), item)
        with self.assertRaises(ValueError):
            self.mapper.item({**self.call(), 'encrypted_function_args': ['opaque']})

    def test_followup_and_message_target_same_native_agent(self):
        for alias, expected in [('native_followup_task', 'followup_task'), ('native_message_task', 'send_message')]:
            item = self.mapper.item(self.call(alias, {'target': '/root/file_probe', 'task_text': '第二轮'}))
            self.assertEqual(item['name'], expected)
            self.assertEqual(json.loads(item['arguments']), {'target': '/root/file_probe', 'message': '第二轮'})

    def test_child_plaintext_conversion_preserves_tool_outputs(self):
        req = {'model': 'deepseek-flash', 'reasoning': {'effort': 'xhigh'}, 'input': [{'type': 'agent_message', 'content': [{'type': 'input_text', 'text': '任务'}]}, {'type': 'function_call_output', 'call_id': 'abc', 'output': 'done'}]}
        out = prepare_request(req, {'routes': ROUTES}, self.history)
        self.assertEqual(out['input'][0]['role'], 'user')
        self.assertEqual(out['input'][1], req['input'][1])
        self.assertEqual(out['reasoning']['effort'], 'high')
        self.assertEqual(req['input'][0]['type'], 'agent_message')
        req['input'][0]['content'] = []
        with self.assertRaises(ValueError): prepare_request(req, {'routes': ROUTES}, self.history)

    def test_invalid_model_effort_and_opaque_task_rejected(self):
        base = {'task_name': 'x', 'task_text': 'task', 'model': 'deepseek-flash', 'reasoning_effort': 'medium'}
        for change in [{'model': 'other'}, {'reasoning_effort': 'ultra'}, {'task_text': 'gAAAAAB...'}, {'task_name': '../x'}]:
            with self.assertRaises(ValueError): native_args('native_delegate_task', {**base, **change}, ROUTES)

    def test_normal_parent_text_streams_without_waiting_for_completion(self):
        event = {'type': 'response.output_text.delta', 'delta': 'hello'}
        self.assertEqual(self.mapper.event(event), [event])

    def http_run(self, model, local_token='test-local-token', content_type=''):
        settings = {'port': 0, 'state_file': str(Path(self.tmp.name) / 'http.json'), 'routes': ROUTES,
                    'parent_base_url': 'http://127.0.0.1:1', 'token_sha256': hashlib.sha256(b'test-local-token').hexdigest()}
        server = Server(settings)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        body = b'event: response.completed\ndata: {"type":"response.completed","response":{"output":[]}}\n\n'
        upstream = io.BytesIO(body); upstream.status = 200; upstream.headers = {'Content-Type': content_type}
        captured = []
        class Opener:
            def open(self, request, **kwargs):
                captured.append(request)
                return upstream
        try:
            with patch('native_gateway.build_opener', return_value=Opener()), patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-ds-only'}):
                request = Request(f'http://127.0.0.1:{server.server_port}/responses', data=json.dumps({'model': model, 'stream': True, 'input': []}).encode(),
                                  headers={'Authorization': 'Bearer test-parent-only', 'X-Native-Adapter-Token': local_token})
                try:
                    with urlopen(request, timeout=3) as response: result = (response.status, response.read())
                except HTTPError as error: result = (error.code, b'')
            return result, captured
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_http_missing_sse_content_type_is_sniffed_and_local_auth_not_forwarded(self):
        (status, body), requests = self.http_run('gpt-6-astra')
        self.assertEqual(status, 200)
        self.assertIn(b'response.completed', body)
        headers = {k.lower(): v for k, v in requests[0].headers.items()}
        self.assertEqual(headers['authorization'], 'Bearer test-parent-only')
        self.assertNotIn('x-native-adapter-token', headers)

    def test_http_bad_local_token_never_calls_upstream(self):
        (status, _), requests = self.http_run('deepseek-flash', local_token='wrong')
        self.assertEqual(status, 401)
        self.assertEqual(requests, [])

    def test_ds_uses_only_selected_ds_credential(self):
        (status, _), requests = self.http_run('deepseek-flash', content_type='text/event-stream')
        self.assertEqual(status, 200)
        self.assertEqual(requests[0].get_header('Authorization'), 'Bearer test-ds-only')
        self.assertEqual(requests[0].full_url, 'https://api.deepseek.com/responses')
        self.assertEqual(json.loads(requests[0].data)['model'], 'deepseek-flash')

    def test_flash_queue_falls_back_after_keep_alive(self):
        class Stream(io.BytesIO):
            headers = {'Content-Type': 'text/event-stream'}
            status = 200
            def close(self): self.was_closed = True
        primary = Stream(b': keep-alive\n\n: keep-alive\n\n')
        fallback = Stream(b'event: response.completed\n')
        requests = []
        class Opener:
            def open(self, request, **kwargs):
                requests.append(json.loads(request.data)); return [primary, fallback][len(requests)-1]
        route = {'queue_fallback_model': 'deepseek-v4-pro', 'queue_fallback_seconds': 20}
        with patch('native_gateway.time.monotonic', side_effect=[0, 12, 24]):
            upstream, first = open_with_queue_fallback(Opener(), 'https://api.deepseek.com/responses', {}, {'model': 'deepseek-flash'}, route)
        self.assertIs(upstream, fallback)
        self.assertIsNone(first)
        self.assertTrue(primary.was_closed)
        self.assertEqual([r['model'] for r in requests], ['deepseek-flash', 'deepseek-v4-pro'])

    def test_desktop_does_not_inherit_external_keys_or_isolation_hooks(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-test', 'CODEX_APP_SERVER_WS_URL': 'ws://test', 'CODEX_CLI_PATH': 'test'}):
            env = desktop_environment({'routes': ROUTES, 'codex_home': 'home'})
        self.assertNotIn('DEEPSEEK_API_KEY', env)
        self.assertNotIn('CODEX_APP_SERVER_WS_URL', env)
        self.assertNotIn('CODEX_CLI_PATH', env)
        self.assertEqual(env['CODEX_HOME'], 'home')

    def test_service_refuses_missing_local_auth(self):
        with self.assertRaises(ValueError): Server({'port': 0})

    def test_configured_high_default_and_explicit_user_override(self):
        routes = {'deepseek-flash': {'efforts': ['low', 'medium', 'high', 'xhigh'], 'default_effort': 'high'}}
        args = {'task_name': 'probe', 'task_text': 'task', 'model': 'deepseek-flash'}
        self.assertEqual(native_args('native_delegate_task', args, routes)['reasoning_effort'], 'high')
        for effort in routes['deepseek-flash']['efforts']:
            self.assertEqual(native_args('native_delegate_task', {**args, 'reasoning_effort': effort}, routes)['reasoning_effort'], effort)

if __name__ == '__main__':
    unittest.main()

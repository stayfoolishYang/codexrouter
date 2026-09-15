"""Local Responses adapter for genuine Codex native external-model subagents.

No credentials or conversation text are persisted. Runtime configuration is local.
"""
import argparse
import copy
import hashlib
import hmac
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

ALIASES = {'native_delegate_task': 'spawn_agent', 'native_followup_task': 'followup_task',
           'native_message_task': 'send_message'}
BUILD_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
ROUTE_LOG_LOCK = threading.Lock()

def log_route(route, requested, selected, queued_seconds):
    path = route.get('route_log')
    if not path:
        return
    record = {'time': time.time(), 'requested': requested, 'selected': selected,
              'queued_seconds': round(queued_seconds, 3), 'fallback': requested != selected}
    with ROUTE_LOG_LOCK, Path(path).open('a', encoding='utf-8') as log:
        log.write(json.dumps(record, separators=(',', ':')) + '\n')

def open_with_queue_fallback(opener, endpoint, headers, req, route):
    requested = req['model']
    request = Request(endpoint, data=json.dumps(req, ensure_ascii=False).encode(), headers=headers)
    upstream = opener.open(request, timeout=660)
    fallback = route.get('queue_fallback_model')
    wait = route.get('queue_fallback_seconds', 0)
    if not fallback or not wait:
        log_route(route, requested, requested, 0)
        return upstream, None
    started = time.monotonic()
    while True:
        line = upstream.readline(4_000_001)
        if line.startswith(b':'):
            upstream.readline(4_000_001)  # blank line terminating the keep-alive frame
            queued = time.monotonic() - started
            if queued < wait:
                continue
            upstream.close()
            fallback_req = copy.deepcopy(req)
            fallback_req['model'] = fallback
            fallback_request = Request(endpoint, data=json.dumps(fallback_req, ensure_ascii=False).encode(), headers=headers)
            log_route(route, requested, fallback, queued)
            return opener.open(fallback_request, timeout=180), None
        log_route(route, requested, requested, time.monotonic() - started)
        return upstream, line

def tools_for(routes):
    result = []
    for name, native in ALIASES.items():
        props = {'task_text': {'type': 'string', 'description': 'The full readable task/message, never ciphertext.'}}
        if native == 'spawn_agent':
            props.update(task_name={'type': 'string'}, model={'type': 'string', 'enum': list(routes)},
                         reasoning_effort={'type': 'string', 'enum': sorted({e for r in routes.values() for e in r.get('efforts', ['medium'])}),
                                           'description': 'User-selected effort. If omitted, use the configured model default.'})
            required = ['task_name', 'task_text', 'model']
        else:
            props['target'] = {'type': 'string'}
            required = ['target', 'task_text']
        result.append({'type': 'function', 'name': name,
                       'description': f'Native Codex {native} with readable external-model message transport. Same native child UI and lifecycle; obey user delegation scope.',
                       'parameters': {'type': 'object', 'properties': props, 'required': required, 'additionalProperties': False}})
    return result

def native_args(alias, args, routes):
    if not isinstance(args, dict):
        raise ValueError('Expected object arguments')
    message = args.get('task_text')
    if not isinstance(message, str) or not message.strip() or message.strip().startswith(('gAAAAA', 'Z0FBQUFB')):
        raise ValueError('Readable task_text is required')
    if alias == 'native_delegate_task':
        model = args.get('model')
        if model not in routes:
            raise ValueError('Unconfigured child model')
        effort = args.get('reasoning_effort', routes[model].get('default_effort', 'medium'))
        if effort not in routes[model].get('efforts', ['medium']):
            raise ValueError('Unsupported configured reasoning effort')
        name = args.get('task_name')
        if not isinstance(name, str) or not re.fullmatch('[a-z0-9_]{1,64}', name):
            raise ValueError('Invalid native task name')
        return {'task_name': name, 'message': message, 'model': model,
                'reasoning_effort': effort, 'fork_turns': 'none'}
    target = args.get('target')
    if not isinstance(target, str) or not target:
        raise ValueError('Native target required')
    return {'target': target, 'message': message}

class History:
    """Only restore calls actually mapped by this adapter, across restarts."""
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.calls = json.loads(self.path.read_text()) if self.path.exists() else {}

    def remember(self, call_id, alias):
        if not call_id:
            raise ValueError('Missing call_id')
        with self.lock:
            if self.calls.get(call_id) == alias:
                return
            self.calls[call_id] = alias
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.calls))
            tmp.replace(self.path)

    def restore(self, item):
        alias = self.calls.get(item.get('call_id'))
        if not alias or item.get('type') != 'function_call':
            return item
        item = copy.deepcopy(item)
        args = json.loads(item['arguments'])
        args['task_text'] = args.pop('message')
        args.pop('fork_turns', None)
        item.update(name=alias, arguments=json.dumps(args, ensure_ascii=False))
        item.pop('namespace', None)
        item.pop('encrypted_function_args', None)
        return item

def prepare_request(req, settings, history):
    req = copy.deepcopy(req)
    if req.get('model') in settings['routes']:
        reasoning = req.get('reasoning')
        if isinstance(reasoning, dict) and reasoning.get('effort'):
            reasoning['effort'] = {
                'minimal': 'low', 'low': 'low', 'medium': 'high',
                'high': 'high', 'xhigh': 'high', 'max': 'max', 'ultra': 'max'
            }.get(reasoning['effort'], reasoning['effort'])
        converted = []
        for item in req.get('input', []):
            if item.get('type') == 'agent_message':
                content = item.get('content', [])
                if not content or any(c.get('type') != 'input_text' for c in content):
                    raise ValueError('Opaque native message cannot be forwarded to external model')
                converted.append({'type': 'message', 'role': 'user', 'content': content})
            else:
                converted.append(item)
        req['input'] = converted
    else:
        req['tools'] = [t for t in req.get('tools', []) if t.get('name') not in ALIASES] + tools_for(settings['routes'])
        req['instructions'] = (req.get('instructions') or '') + (
            '\nExternal native subagents: when the user authorizes delegation to a configured external model, '
            'use native_delegate_task with explicit plaintext task_text and model. This maps to genuine '
            'Codex collaboration.spawn_agent with fork_turns=none. For later messages use native_message_task '
            'or native_followup_task. Use ordinary collaboration wait/list/interrupt tools for lifecycle. '
            'Do not call collaboration.spawn_agent directly for external models, since its message may be '
            'encrypted upstream. Include needed context and authorized file boundaries in task_text. '
            'Do not delegate unless authorized. Do not silently substitute a CLI worker or another model. '
            'Ordinary GPT subagents retain their existing native tools.')
        req['input'] = [history.restore(item) for item in req.get('input', [])]
    return req

class EventMapper:
    def __init__(self, routes, history):
        self.routes, self.history = routes, history
        self.pending = {}
        self.alias_ids = set()

    def item(self, value):
        if value.get('type') != 'function_call' or value.get('name') not in ALIASES:
            return value
        if value.get('encrypted_function_args') not in (None, []):
            raise ValueError('Encrypted alias cannot be translated')
        alias = value['name']
        args = native_args(alias, json.loads(value['arguments']), self.routes)
        self.history.remember(value.get('call_id'), alias)
        value = copy.deepcopy(value)
        value.update(name=ALIASES[alias], namespace='collaboration', encrypted_function_args=[],
                     arguments=json.dumps(args, ensure_ascii=False))
        return value

    def event(self, event):
        kind = event.get('type', '')
        item = event.get('item', {})
        if kind == 'response.output_item.added' and item.get('name') in ALIASES:
            ident = item.get('id')
            self.pending[ident] = event
            self.alias_ids.add(ident)
            return []
        if kind.startswith('response.function_call_arguments.') and event.get('item_id') in self.alias_ids:
            return []  # Native arguments are emitted atomically when their complete JSON is known.
        if kind == 'response.output_item.done' and item.get('name') in ALIASES:
            mapped = self.item(item)
            events = []
            previous = self.pending.pop(item.get('id'), None)
            if previous:
                previous = copy.deepcopy(previous)
                previous['item'] = {**mapped, 'arguments': ''}
                events.append(previous)
            events.append({**event, 'item': mapped})
            return events
        if isinstance(event.get('response'), dict):
            event = copy.deepcopy(event)
            event['response']['output'] = [self.item(v) for v in event['response'].get('output', [])]
        return [event]

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, settings):
        if not re.fullmatch('[0-9a-f]{64}', settings.get('token_sha256', '')):
            raise ValueError('A local adapter authentication token hash is required')
        self.settings = settings
        self.history = History(settings['state_file'])
        super().__init__(('127.0.0.1', settings['port']), Handler)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/healthz':
            body = json.dumps({'service': 'codex-native-model-adapter', 'version': 1,
                               'instance_id': self.server.settings.get('instance_id'),
                               'build_sha256': BUILD_SHA256,
                               'models': list(self.server.settings['routes'])}).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path.rstrip('/') != '/responses':
            self.send_error(404); return
        if not self.headers.get('Authorization', '').startswith('Bearer '):
            self.send_error(401); return
        token_hash = self.server.settings.get('token_sha256')
        if not hmac.compare_digest(hashlib.sha256(self.headers.get('X-Native-Adapter-Token', '').encode()).hexdigest(), token_hash):
            self.send_error(401); return
        started = False
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 32_000_000:
                self.send_error(413); return
            incoming = json.loads(self.rfile.read(size))
            settings = self.server.settings
            external = incoming.get('model') in settings['routes']
            req = prepare_request(incoming, settings, self.server.history)
            headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
            if external:
                route = settings['routes'][incoming['model']]
                req['model'] = route.get('upstream_model', incoming['model'])
                key = os.environ.get(route['key_env'])
                if not key:
                    self.send_error(503, 'Configured model key environment variable is unavailable'); return
                endpoint = route['base_url'].rstrip('/') + '/responses'
                headers['Authorization'] = 'Bearer ' + key
            else:
                endpoint = settings['parent_base_url'].rstrip('/') + '/responses'
                # Reuse the requesting Codex runtime's auth; never read/copy its credential store.
                for name, value in self.headers.items():
                    if name.lower() not in {'host', 'content-length', 'connection', 'accept-encoding', 'transfer-encoding', 'x-native-adapter-token'}:
                        headers[name] = value
            opener = build_opener(NoRedirect())
            if external:
                upstream, first_line = open_with_queue_fallback(opener, endpoint, headers, req, route)
            else:
                request = Request(endpoint, data=json.dumps(req, ensure_ascii=False).encode(), headers=headers)
                upstream, first_line = opener.open(request, timeout=180), None
            with upstream:
                content_type = upstream.headers.get('Content-Type', '')
                if not content_type:
                    first_line = first_line or upstream.readline(4_000_001)
                    if not first_line.startswith((b'event:', b'data:', b':')):
                        self.send_error(502, 'Upstream response is not an SSE stream'); return
                elif 'text/event-stream' not in content_type:
                    self.send_error(502, 'Expected upstream Responses SSE stream'); return
                self.send_response(upstream.status)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                for name in ('x-request-id', 'openai-processing-ms'):
                    if upstream.headers.get(name): self.send_header(name, upstream.headers[name])
                self.end_headers(); started = True; self.close_connection = True
                mapper = EventMapper(settings['routes'], self.server.history)
                frame = []
                while True:
                    if first_line is not None:
                        line, first_line = first_line, None
                    else:
                        line = upstream.readline(4_000_001)
                    if not line: break
                    if len(line) > 4_000_000: raise ValueError('Oversized SSE line')
                    if line.strip():
                        frame.append(line)
                        if sum(map(len, frame)) > 4_000_000: raise ValueError('Oversized SSE frame')
                        continue
                    data = b'\n'.join(v[5:].strip() for v in frame if v.startswith(b'data:'))
                    if external:
                        self.wfile.write(b''.join(frame) + b'\n'); self.wfile.flush()
                        terminal = (data == b'[DONE]')
                        if data and data != b'[DONE]':
                            terminal = json.loads(data).get('type') in {
                                'response.completed', 'response.failed', 'response.incomplete'
                            }
                        frame = []
                        if terminal:
                            break
                        continue
                    if data and data != b'[DONE]':
                        for event in mapper.event(json.loads(data)):
                            self.wfile.write(('event: ' + event.get('type', 'message') + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode())
                    else:
                        self.wfile.write(b''.join(frame) + b'\n')
                    frame = []; self.wfile.flush()
                if frame: raise ValueError('Incomplete upstream SSE frame')
        except HTTPError as error:
            if not started: self.send_error(error.code, 'Selected upstream request failed')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            if not started: self.send_error(502, type(error).__name__)
            else:
                try:
                    self.wfile.write(b'event: error\ndata: {"type":"error","message":"Native adapter stream failed"}\n\n')
                except OSError:
                    pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    settings = json.loads(Path(args.config).read_text())
    for url in [settings['parent_base_url']] + [r['base_url'] for r in settings['routes'].values()]:
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Use credential-free base URLs')
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')):
            raise ValueError('Upstream must use HTTPS or loopback HTTP')
    Path(settings['state_file']).parent.mkdir(parents=True, exist_ok=True)
    Server(settings).serve_forever()

if __name__ == '__main__':
    main()

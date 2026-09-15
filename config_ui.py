"""Ephemeral loopback UI for configuring CodexRouter without storing API keys."""
import argparse
from contextlib import nullcontext
import datetime
import hmac
import json
import os
import re
import secrets
import threading
import tomllib
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, build_opener

from native_gateway import NoRedirect
from native_install import atomic_write
from native_launcher import catalog

EFFORTS = ('low', 'medium', 'high', 'xhigh')
MODEL_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}')
ENV_PATTERN = re.compile(r'[A-Z_][A-Z0-9_]{1,63}')


def validate_base_url(value):
    value = str(value or '').strip().rstrip('/')
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('网关地址不能包含凭据、查询参数或片段')
    loopback = parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')
    if parsed.scheme != 'https' and not loopback:
        raise ValueError('网关必须使用 HTTPS；本机地址可使用 HTTP')
    if not parsed.hostname:
        raise ValueError('网关地址无效')
    return value


def validate_model_id(value):
    value = str(value or '').strip()
    if not MODEL_PATTERN.fullmatch(value):
        raise ValueError('模型 ID 只能包含字母、数字、点、下划线、斜杠、冒号和连字符')
    return value


def validate_env_name(value):
    value = str(value or '').strip().upper()
    if not ENV_PATTERN.fullmatch(value):
        raise ValueError('密钥变量需使用大写字母、数字和下划线')
    return value


def normalize_efforts(values, default):
    values = [value for value in EFFORTS if value in set(values or [])]
    default = str(default or '')
    if not values:
        raise ValueError('至少选择一个支持的推理档位')
    if default not in values:
        raise ValueError('默认档位必须包含在支持档位中')
    return values, default


def get_user_secret(name):
    if os.name != 'nt':
        return os.environ.get(name, '')
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except FileNotFoundError:
        return os.environ.get(name, '')


def set_user_secret(name, value):
    if not value:
        return
    if os.name == 'nt':
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    os.environ[name] = value


def fetch_model_ids(base_url, api_key, timeout=20):
    base_url = validate_base_url(base_url)
    if not api_key:
        raise ValueError('请填写 API Key，或选择已有密钥变量')
    request = Request(base_url + '/models', headers={
        'Accept': 'application/json', 'Authorization': 'Bearer ' + api_key,
        'User-Agent': 'CodexRouter/1',
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as error:
        raise ValueError(f'模型接口返回 HTTP {error.code}') from None
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise ValueError('无法读取模型列表：' + type(error).__name__) from None
    items = payload.get('data', payload.get('models', [])) if isinstance(payload, dict) else []
    found = []
    for item in items:
        value = item.get('id') if isinstance(item, dict) else item
        try:
            value = validate_model_id(value)
        except ValueError:
            continue
        if value not in found:
            found.append(value)
    if not found:
        raise ValueError('接口未返回可用的模型 ID')
    return found[:500]


class ConfigStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.settings_path = self.root / 'settings.json'
        self.ui_path = self.root / 'config_ui.html'
        self.lock = threading.Lock()

    def load_settings(self):
        return json.loads(self.settings_path.read_text(encoding='utf-8'))

    def config_path(self, settings):
        return Path(settings['codex_home']) / 'config.toml'

    def source_models(self, settings):
        source = Path(settings['source_catalog'])
        if not source.is_file():
            source = Path(settings['catalog_file'])
        payload = json.loads(source.read_text(encoding='utf-8-sig'))
        routes = set(settings.get('routes', {}))
        result = []
        for model in payload.get('models', []):
            model_id = model.get('slug')
            if model_id and model_id not in routes:
                result.append({'id': model_id, 'name': model.get('display_name') or model_id})
        return result

    def state(self):
        settings = self.load_settings()
        config = tomllib.loads(self.config_path(settings).read_text(encoding='utf-8'))
        routes = []
        for model_id, route in settings.get('routes', {}).items():
            routes.append({
                'model_id': model_id,
                'provider_name': route.get('provider_name', ''),
                'base_url': route['base_url'],
                'key_env': route['key_env'],
                'key_configured': bool(get_user_secret(route['key_env'])),
                'efforts': route.get('efforts', ['medium']),
                'default_effort': route.get('default_effort', 'medium'),
            })
        routes.sort(key=lambda item: item['model_id'].lower())
        default_external = routes[0]['model_id'] if routes else ''
        review = settings.get('review', {'mode': 'terra'})
        subagent = settings.get('subagent', {'model': default_external, 'effort': 'high'})
        return {
            'current_model': config.get('model', ''),
            'primary_models': self.source_models(settings),
            'routes': routes,
            'review': {
                'mode': review.get('mode', 'terra'),
                'model': review.get('model', default_external),
                'effort': review.get('effort', 'high'),
            },
            'subagent': {
                'model': subagent.get('model', default_external),
                'effort': subagent.get('effort', 'high'),
            },
            'restart_required': True,
        }

    def _save_settings(self, settings):
        backup = self.root / 'backups' / ('ui-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-settings.json')
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(self.settings_path.read_bytes())
        atomic_write(self.settings_path, json.dumps(settings, ensure_ascii=False, indent=2).encode('utf-8'))
        try:
            catalog(settings)
        except Exception:
            atomic_write(self.settings_path, backup.read_bytes())
            raise

    def save_route(self, payload):
        with self.lock:
            settings = self.load_settings()
            model_id = validate_model_id(payload.get('model_id'))
            original_id = str(payload.get('original_model_id') or model_id)
            base_url = validate_base_url(payload.get('base_url'))
            key_env = validate_env_name(payload.get('key_env'))
            efforts, default = normalize_efforts(payload.get('efforts'), payload.get('default_effort'))
            routes = dict(settings.get('routes', {}))
            if original_id != model_id and model_id in routes:
                raise ValueError('该模型 ID 已存在')
            existing = dict(routes.get(original_id, {}))
            existing.update({
                'base_url': base_url,
                'key_env': key_env,
                'provider_name': str(payload.get('provider_name') or '').strip()[:80],
                'upstream_model': model_id,
                'efforts': efforts,
                'default_effort': default,
                'route_log': existing.get('route_log', str(self.root / 'route-audit.jsonl')),
            })
            if original_id != model_id:
                routes.pop(original_id, None)
            routes[model_id] = existing
            settings['routes'] = routes
            if original_id != model_id:
                review = settings.get('review', {})
                if review.get('model') == original_id:
                    review['model'] = model_id
                subagent = settings.get('subagent', {})
                if subagent.get('model') == original_id:
                    subagent['model'] = model_id
            set_user_secret(key_env, str(payload.get('api_key') or '').strip())
            if not get_user_secret(key_env):
                raise ValueError('请填写 API Key，或使用已经配置的环境变量')
            self._save_settings(settings)
            if original_id != model_id:
                config = tomllib.loads(self.config_path(settings).read_text(encoding='utf-8'))
                if config.get('model') == original_id:
                    self.save_primary_model({'model': model_id}, _locked=True)
            return {'message': '模型已保存。完全重启 Codex 后生效。'}

    def delete_route(self, payload):
        with self.lock:
            settings = self.load_settings()
            model_id = validate_model_id(payload.get('model_id'))
            config = tomllib.loads(self.config_path(settings).read_text(encoding='utf-8'))
            if config.get('model') == model_id:
                raise ValueError('该模型正被主代理使用，请先切换主代理模型')
            if settings.get('review', {}).get('mode') == 'external' and settings['review'].get('model') == model_id:
                raise ValueError('该模型正被外部审阅模式使用，请先切换审阅模式')
            if settings.get('subagent', {}).get('model') == model_id:
                raise ValueError('该模型是默认外部子代理，请先修改默认模型')
            if model_id not in settings.get('routes', {}):
                raise ValueError('模型不存在')
            settings['routes'].pop(model_id)
            self._save_settings(settings)
            return {'message': '模型已删除。关联的环境变量未删除。'}

    def save_preferences(self, payload):
        with self.lock:
            settings = self.load_settings()
            routes = settings.get('routes', {})
            mode = payload.get('review_mode')
            if mode not in ('primary', 'terra', 'external'):
                raise ValueError('审阅模式无效')
            review_model = str(payload.get('review_model') or '')
            review_effort = str(payload.get('review_effort') or 'high')
            subagent_model = str(payload.get('subagent_model') or '')
            subagent_effort = str(payload.get('subagent_effort') or 'high')
            if mode == 'external':
                self._validate_route_choice(routes, review_model, review_effort, '审阅模型')
            if subagent_model:
                self._validate_route_choice(routes, subagent_model, subagent_effort, '默认子代理')
            settings['review'] = {'mode': mode, 'model': review_model, 'effort': review_effort}
            settings['subagent'] = {'model': subagent_model, 'effort': subagent_effort}
            self._save_settings(settings)
            return {'message': '审阅和子代理偏好已保存。完全重启 Codex 后生效。'}

    @staticmethod
    def _validate_route_choice(routes, model_id, effort, label):
        if model_id not in routes:
            raise ValueError(label + '未选择有效的外部模型')
        if effort not in routes[model_id].get('efforts', ['medium']):
            raise ValueError(label + '不支持所选推理档位')

    def save_primary_model(self, payload, _locked=False):
        lock = nullcontext() if _locked else self.lock
        with lock:
            settings = self.load_settings()
            model_id = validate_model_id(payload.get('model'))
            allowed = {item['id'] for item in self.source_models(settings)} | set(settings.get('routes', {}))
            if model_id not in allowed:
                raise ValueError('主代理模型不在当前模型目录中')
            path = self.config_path(settings)
            original = path.read_text(encoding='utf-8')
            line = 'model = ' + json.dumps(model_id)
            rendered, count = re.subn(r'^model\s*=.*$', lambda _: line, original, count=1, flags=re.M)
            if count != 1:
                rendered = line + '\n' + original
            tomllib.loads(rendered)
            backup = self.root / 'backups' / ('ui-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-config.toml')
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(original, encoding='utf-8')
            atomic_write(path, rendered.encode('utf-8'))
            self._update_manifest_hash(path)
            return {'message': '主代理模型已切换。完全重启 Codex 后生效。'}

    def _update_manifest_hash(self, target):
        import hashlib
        manifest = self.root / 'deployment.json'
        if not manifest.exists():
            return
        data = json.loads(manifest.read_text(encoding='utf-8'))
        for entry in data.get('files', []):
            if Path(entry['target']) == target:
                entry['installed_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
        atomic_write(manifest, json.dumps(data, indent=2).encode('utf-8'))


class ConfigServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, store, token):
        self.store, self.token = store, token
        super().__init__(('127.0.0.1', 0), ConfigHandler)


class ConfigHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _headers(self, content_type, size):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(size))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()

    def _authorized(self):
        return hmac.compare_digest(self.headers.get('X-CodexRouter-UI', ''), self.server.token)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path == '/':
            supplied = parse_qs(parsed.query).get('token', [''])[0]
            if not hmac.compare_digest(supplied, self.server.token):
                self.send_error(403)
                return
            html = self.server.store.ui_path.read_text(encoding='utf-8').replace('__UI_TOKEN__', json.dumps(self.server.token))
            body = html.encode('utf-8')
            self._headers('text/html; charset=utf-8', len(body))
            self.wfile.write(body)
            return
        if parsed.path == '/api/state' and self._authorized():
            try:
                self._json(200, self.server.store.state())
            except Exception as error:
                self._json(500, {'error': str(error)})
            return
        self.send_error(404)

    def do_POST(self):
        if not self._authorized():
            self._json(403, {'error': '配置会话无效，请重新打开设置界面'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 1_000_000:
                raise ValueError('请求过大')
            payload = json.loads(self.rfile.read(size) or b'{}')
            actions = {
                '/api/discover': self._discover,
                '/api/route/save': self.server.store.save_route,
                '/api/route/delete': self.server.store.delete_route,
                '/api/preferences': self.server.store.save_preferences,
                '/api/primary': self.server.store.save_primary_model,
            }
            if self.path == '/api/close':
                self._json(200, {'message': '设置服务已关闭'})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            action = actions.get(self.path)
            if not action:
                self._json(404, {'error': '未知操作'})
                return
            self._json(200, action(payload))
        except (ValueError, KeyError) as error:
            self._json(400, {'error': str(error)})
        except Exception as error:
            self._json(500, {'error': type(error).__name__ + ': ' + str(error)})

    @staticmethod
    def _discover(payload):
        key = str(payload.get('api_key') or '').strip()
        if not key and payload.get('key_env'):
            key = get_user_secret(validate_env_name(payload['key_env']))
        return {'models': fetch_model_ids(payload.get('base_url'), key)}


def run(root, open_browser=True):
    store = ConfigStore(root)
    if not store.settings_path.is_file() or not store.ui_path.is_file():
        raise RuntimeError('CodexRouter UI files are not installed')
    token = secrets.token_urlsafe(32)
    server = ConfigServer(store, token)
    url = f'http://127.0.0.1:{server.server_port}/?token={token}'
    print(url, flush=True)
    if open_browser:
        webbrowser.open(url)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description='Open the local CodexRouter configuration UI.')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--no-browser', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    run(args.root, open_browser=not args.no_browser)


if __name__ == '__main__':
    main()

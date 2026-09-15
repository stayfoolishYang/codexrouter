"""Start the installed adapter before opening the ordinary Codex desktop profile."""
import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
import re
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent

def reconcile_config(settings):
    """Restore only the known route, never overwrite an unrelated provider switch."""
    from native_install import render_config, atomic_write
    path = Path(settings['codex_home']) / 'config.toml'
    original = path.read_text(encoding='utf-8')
    config = tomllib.loads(original)
    provider = settings['original_provider']
    info = config.get('model_providers', {}).get(provider)
    if info is None:
        raise RuntimeError('Original upstream provider was removed; choose the intended parent route before reconnecting native DS. Current configuration preserved.')
    local = f"http://127.0.0.1:{settings['port']}"
    if config.get('model_provider') != provider or info.get('base_url') not in (local, settings['parent_base_url']):
        raise RuntimeError('Provider changed to an unrelated route; configuration preserved.')
    token = (ROOT / 'adapter-token.txt').read_text(encoding='utf-8').strip()
    if hashlib.sha256(token.encode()).hexdigest() != settings['token_sha256']:
        raise RuntimeError('Adapter token does not match settings; configuration preserved.')
    headers = info.get('http_headers', {})
    if headers and headers != {'X-Native-Adapter-Token': token}:
        raise RuntimeError('Custom provider headers changed; configuration preserved.')
    text = original
    if headers:
        pattern = r'(?ms)(^\[model_providers\.' + re.escape(provider) + r'\]\s*\n)(.*?)(?=^\[|\Z)'
        def strip_header(match):
            block, count = re.subn(r'^http_headers\s*=\s*\{[^\n]*\}\s*$', '', match.group(2), count=1, flags=re.M)
            if count != 1:
                raise RuntimeError('Provider header format needs manual review.')
            return match.group(1) + block
        text = re.sub(pattern, strip_header, text)
    rendered = render_config(text, provider, settings, token)
    if tomllib.loads(rendered) == config:
        return False
    backup = ROOT / 'backups' / ('launch-' + str(time.time_ns()) + '.toml')
    backup.parent.mkdir(exist_ok=True)
    backup.write_text(original, encoding='utf-8')
    if path.read_text(encoding='utf-8') != original:
        raise RuntimeError('Configuration changed during repair; retry launch.')
    atomic_write(path, rendered.encode('utf-8'))
    manifest = ROOT / 'deployment.json'
    if manifest.exists():
        deployment = json.loads(manifest.read_text())
        for entry in deployment['files']:
            if Path(entry['target']) == path:
                entry['installed_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        atomic_write(manifest, json.dumps(deployment, indent=2).encode())
    return True

def desktop_environment(settings):
    env = dict(os.environ)
    for name in ('CODEX_APP_SERVER_WS_URL', 'CODEX_ELECTRON_USER_DATA_PATH', 'CODEX_APP_SERVER_FORCE_CLI', 'CODEX_CLI_PATH'):
        env.pop(name, None)
    for route in settings['routes'].values():
        env.pop(route['key_env'], None)
    env['CODEX_HOME'] = settings['codex_home']
    return env

def check(settings):
    try:
        with urlopen(f"http://127.0.0.1:{settings['port']}/healthz", timeout=1) as response:
            health = json.load(response)
        if health.get('instance_id') != settings['instance_id']:
            raise RuntimeError('Adapter port belongs to a different service')
        expected = hashlib.sha256((ROOT / 'native_gateway.py').read_bytes()).hexdigest()
        if health.get('build_sha256') != expected:
            raise RuntimeError('Running adapter build is outdated. Finish running tasks before restarting the adapter.')
        return True
    except OSError:
        return False

def catalog(settings):
    source = Path(settings['source_catalog'])
    if not source.is_file():
        source = Path(settings['catalog_file'])
    data = json.loads(source.read_text(encoding='utf-8-sig'))
    models = data['models']
    template = next((m for m in models if m['slug'] == 'gpt-5.5'), models[0])
    for model in settings['routes']:
        models[:] = [m for m in models if m['slug'] != model]
        child = copy.deepcopy(template)
        provider_name = settings['routes'][model].get('provider_name', 'external')
        child.update(slug=model, display_name=model + ' (' + provider_name + ')', description='External model through local Responses adapter',
                     visibility='list', supported_in_api=True, default_reasoning_level=settings['routes'][model].get('default_effort', 'medium'),
                     supported_reasoning_levels=[{'effort': e, 'description': e} for e in settings['routes'][model].get('efforts', ['medium'])],
                     base_instructions='You are a coding agent running through Codex. Follow the task, project instructions, tool permission checks and sandbox limits. Use available tools to inspect and edit only authorized files. On Windows use PowerShell, and never invoke apply_patch as a shell command; use a provided apply_patch tool if present or write files through supported shell operations. Report verified changes, tests and limitations. Do not access secrets. Do not delegate without authorization.')
        child.pop('model_messages', None)
        child.pop('upgrade', None)
        child['service_tiers'] = []
        child['additional_speed_tiers'] = []
        models.append(child)
    target = Path(settings['catalog_file'])
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    tmp.replace(target)

def start(settings):
    if check(settings):
        return
    if os.name == 'nt' and settings.get('scheduled_task'):
        subprocess.run(['schtasks.exe', '/Run', '/TN', settings['scheduled_task']],
                       check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(100):
            if check(settings):
                return
            time.sleep(.1)
        raise RuntimeError('Scheduled adapter readiness timeout; see launcher-errors.log and Task Scheduler.')
    raise RuntimeError('Adapter scheduled task is missing; repair the installation.')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'open', 'ui', 'repair', 'serve', 'status', 'rollback'])
    args = parser.parse_args()
    settings = json.loads((ROOT / 'settings.json').read_text())
    if args.action == 'serve':
        # Task Scheduler owns the gateway process, outside the desktop process tree.
        import winreg
        for route in settings['routes'].values():
            name = route['key_env']
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
                try:
                    os.environ[name] = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    if not os.environ.get(name):
                        raise RuntimeError('Configured model credential unavailable: ' + name)
        (ROOT / 'service-pid.json').write_text(json.dumps({'pid': os.getpid(), 'started': time.time(), 'host': 'task-scheduler'}))
        import native_gateway
        sys.argv = [str(ROOT / 'native_gateway.py'), '--config', str(ROOT / 'settings.json')]
        native_gateway.main()
        return
    if args.action == 'rollback':
        deployment = json.loads((ROOT / 'deployment.json').read_text())
        for entry in deployment['files']:
            target = Path(entry['target'])
            if hashlib.sha256(target.read_bytes()).hexdigest() != entry['installed_sha256']:
                raise RuntimeError('File changed since installation; preserve new edits and restore manually from ' + deployment['backup_dir'])
        for entry in deployment['files']:
            target = Path(entry['target'])
            tmp = target.with_name(target.name + '.native-rollback.tmp')
            tmp.write_bytes(Path(entry['backup']).read_bytes())
            tmp.replace(target)
        for shortcut in deployment.get('shortcuts', []):
            Path(shortcut).unlink(missing_ok=True)
        if settings.get('scheduled_task'):
            from native_install import delete_service_task
            delete_service_task(settings['scheduled_task'])
        print('Previous configuration restored. Fully exit and reopen Codex to load it. Adapter kept alive for any running tasks.')
        return
    if args.action == 'status':
        print(json.dumps({'ready': check(settings), 'port': settings['port'], 'models': list(settings['routes'])}))
        return
    if args.action == 'ui':
        import config_ui
        config_ui.run(ROOT)
        return
    catalog(settings)
    start(settings)
    if args.action in ('open', 'repair'):
        reconcile_config(settings)
    if args.action == 'open':
        config = tomllib.loads((Path(settings['codex_home']) / 'config.toml').read_text())
        provider = config['model_providers'][settings['original_provider']]
        header = provider.get('http_headers', {}).get('X-Native-Adapter-Token', '')
        if (config.get('model_catalog_json') != settings['catalog_file']
            or provider.get('base_url') != f"http://127.0.0.1:{settings['port']}"
            or hashlib.sha256(header.encode()).hexdigest() != settings.get('token_sha256')
            or provider.get('request_max_retries') != 0 or provider.get('stream_max_retries') != 0
            or provider.get('supports_websockets') is not False):
            raise RuntimeError('Native adapter configuration changed, possibly by provider management. Reapply the reviewed installation before opening.')
        desktop_exe = Path(settings['desktop_exe'])
        if not desktop_exe.exists() and os.name == 'nt':
            probe = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                                    'Get-AppxPackage -Name OpenAI.Codex | Select-Object -First 1 -ExpandProperty InstallLocation'],
                                   capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            desktop_exe = Path(probe.stdout.strip()) / 'app' / 'ChatGPT.exe'
        if not desktop_exe.is_file():
            raise RuntimeError('Installed Codex executable not found; refresh its installation path.')
        env = desktop_environment(settings)
        subprocess.Popen([str(desktop_exe)], env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # pythonw and scheduled-task invocations have no console; retain a credential-free error.
        with (ROOT / 'launcher-errors.log').open('a', encoding='utf-8') as log:
            log.write(type(error).__name__ + ': ' + str(error) + '\n')
        if os.name == 'nt' and any(action in sys.argv for action in ('open', 'ui')):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, 'Native DS startup failed. See:\n' + str(ROOT / 'launcher-errors.log'), 'Codex Native DS', 16)
        raise

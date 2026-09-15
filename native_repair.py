"""Upgrade an existing, stopped adapter; preserve credentials and back up changes."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tomllib

from native_install import atomic_write, delete_service_task, register_service_task, settings_shortcut, stop_owned_service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    if not args.activate:
        parser.print_help()
        return
    root = Path.home() / '.codex' / 'native-model-adapter'
    settings = json.loads((root / 'settings.json').read_text())
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', settings['port'])) == 0:
            raise RuntimeError('Adapter port is active; finish tasks and stop the owned service before upgrade.')
    backup = root / 'backups' / ('repair-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True)
    names = ['native_launcher.py', 'native_gateway.py', 'native_install.py', 'config_ui.py', 'config_ui.html', 'settings.json', 'adapter-token.txt', 'deployment.json']
    for name in names:
        if (root / name).exists():
            shutil.copy2(root / name, backup / name)
    shutil.copy2(Path(settings['codex_home']) / 'config.toml', backup / 'config.toml')
    token_path = root / 'adapter-token.txt'
    config = tomllib.loads((Path(settings['codex_home']) / 'config.toml').read_text())
    token = token_path.read_text().strip() if token_path.exists() else config['model_providers'][settings['original_provider']].get('http_headers', {}).get('X-Native-Adapter-Token')
    if token and hashlib.sha256(token.encode()).hexdigest() != settings['token_sha256']:
        raise RuntimeError('Existing adapter token mismatch; files preserved.')
    token = token or secrets.token_urlsafe(32)
    settings['token_sha256'] = hashlib.sha256(token.encode()).hexdigest()
    for name in ['native_launcher.py', 'native_gateway.py', 'native_install.py', 'config_ui.py', 'config_ui.html']:
        atomic_write(root / name, (Path(__file__).parent / name).read_bytes())
    atomic_write(root / 'adapter-token.txt', token.encode())
    registered = not settings.get('scheduled_task')
    try:
        if registered:
            register_service_task(root, settings)
        atomic_write(root / 'settings.json', json.dumps(settings, indent=2).encode())
        subprocess.run([sys.executable, str(root / 'native_launcher.py'), 'repair'], check=True)
    except Exception as error:
        if registered and settings.get('scheduled_task'):
            try:
                stop_owned_service(root)
                delete_service_task(settings['scheduled_task'])
            except Exception as cleanup_error:
                error.add_note('Failed to remove the partial scheduled task: ' + str(cleanup_error))
        raise
    shortcut = settings_shortcut(root, settings['desktop_exe'])
    manifest = root / 'deployment.json'
    if manifest.exists():
        deployment = json.loads(manifest.read_text())
        if shortcut not in deployment.setdefault('shortcuts', []):
            deployment['shortcuts'].append(shortcut)
            atomic_write(manifest, json.dumps(deployment, indent=2).encode())
    print('Adapter repaired; backup: ' + str(backup))
    print('The current desktop backend still requires a full reload to use restored routing.')


if __name__ == '__main__':
    main()

"""Install the native adapter on Windows with backups for replaced user files."""
import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parent

RULES = '''## DeepSeek Delegation (Native Official Adapter)

- When the user explicitly requests a DS/DeepSeek subagent, use the provided native_delegate_task tool with model=deepseek-flash and a unique task_name. Default reasoning_effort is high; an explicit user choice of low, medium, high or xhigh overrides the default. Put readable task instructions, required context, authorized file paths and acceptance criteria in task_text. Mentioning a model alone does not authorize delegation.
- The adapter maps this to genuine Codex collaboration.spawn_agent with fork_turns=none. DeepSeek goes directly to its configured official API through the local adapter. The primary model retains its existing provider route.
- Use native_followup_task for later work on the same child and native_message_task for a running child. Use available native collaboration wait/list/interrupt tools for lifecycle. Independently verify returned files and tests. Initial delegation, file reading/writing and idle followup have been verified; running-message delivery, stopping and concurrency require actual verification.
- If plaintext alias tools are absent or the model is unknown, report that the current backend has not loaded integration. Fully quit Codex and reopen through the native DS launcher. Do not silently substitute another transport or reinterpret ciphertext as plaintext.
- Preserve unrelated model preferences and never print credentials or task content.

'''

def atomic_write(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.native-install.tmp')
    tmp.write_bytes(data)
    tmp.replace(path)

def render_config(text, provider, settings, token):
    config = tomllib.loads(text)
    info = config['model_providers'][provider]
    if info.get('http_headers') or info.get('env_http_headers'):
        raise ValueError('Existing custom provider headers need a manual merge; no configuration changed')
    line = 'model_catalog_json = ' + json.dumps(settings['catalog_file'])
    if 'model_catalog_json' in config:
        text = re.sub(r'^model_catalog_json\s*=.*$', lambda _: line, text, count=1, flags=re.M)
    else:
        text = line + '\n' + text
    pattern = r'(?ms)^\[model_providers\.' + re.escape(provider) + r'\]\s*\n(.*?)(?=^\[|\Z)'
    match = re.search(pattern, text)
    if not match:
        raise ValueError('Provider section format needs manual review')
    block = match.group(0).rstrip() + '\n'
    fields = {'base_url': json.dumps('http://127.0.0.1:' + str(settings['port'])),
              'supports_websockets': 'false', 'request_max_retries': '0', 'stream_max_retries': '0'}
    for key, value in fields.items():
        if re.search(r'^' + key + r'\s*=', block, re.M):
            block = re.sub(r'^' + key + r'\s*=.*$', lambda _, k=key, v=value: k + ' = ' + v, block, flags=re.M)
        else:
            block += key + ' = ' + value + '\n'
    block += 'http_headers = { "X-Native-Adapter-Token" = ' + json.dumps(token) + ' }\n\n'
    rendered = text[:match.start()] + block + text[match.end():]
    result = tomllib.loads(rendered)
    assert result.get('model') == config.get('model')
    assert result['model_provider'] == config['model_provider']
    return rendered

def render_rules(text):
    if '## DeepSeek Delegation' in text:
        start = text.index('## DeepSeek Delegation')
        following = re.search(r'^## ', text[start+3:], re.M)
        end = start + 3 + following.start() if following else len(text)
        return text[:start] + RULES + text[end:]
    return text.rstrip() + '\n\n' + RULES

def ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"

def register_service_task(root, settings):
    name = 'Codex Native Model Adapter ' + settings['instance_id']
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():
        raise RuntimeError('pythonw.exe unavailable for hidden service hosting')
    code = (
        f'$nativeTaskName = {ps_quote(name)}; '
        '$nativeUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name; '
        '$nativeExisting = Get-ScheduledTask -TaskName $nativeTaskName -ErrorAction SilentlyContinue; '
        'if ($nativeExisting) { throw "Adapter task already exists; inspect before replacing" }; '
        f'$nativeAction = New-ScheduledTaskAction -Execute {ps_quote(pythonw)} '
        f'-Argument {ps_quote(chr(34)+str(root / "native_launcher.py")+chr(34)+" serve")} -WorkingDirectory {ps_quote(root)}; '
        '$nativeTrigger = New-ScheduledTaskTrigger -AtLogOn -User $nativeUser; '
        '$nativePrincipal = New-ScheduledTaskPrincipal -UserId $nativeUser -LogonType Interactive -RunLevel Limited; '
        '$nativeOptions = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) '
        '-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew '
        '-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; '
        'Register-ScheduledTask -TaskName $nativeTaskName -Action $nativeAction -Trigger $nativeTrigger '
        '-Principal $nativePrincipal -Settings $nativeOptions -ErrorAction Stop | Out-Null')
    subprocess.run(['powershell.exe', '-NoProfile', '-Command', code], check=True,
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    settings['scheduled_task'] = name

def delete_service_task(name):
    subprocess.run(['schtasks.exe', '/Delete', '/TN', name, '/F'], check=True,
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)

def stop_owned_service(root):
    pid_file = root / 'service-pid.json'
    if not pid_file.exists():
        return
    pid = int(json.loads(pid_file.read_text())['pid'])
    script = str(root / 'native_gateway.py')
    code = (f'$nativeAdapterProcess = Get-CimInstance Win32_Process -Filter "ProcessId = {pid}"; '
            'if ($nativeAdapterProcess) { '
            f'if (-not $nativeAdapterProcess.CommandLine.Contains({ps_quote(script)})) {{ throw "Process identity mismatch" }}; '
            f'Stop-Process -Id {pid}; }}')
    subprocess.run(['powershell.exe', '-NoProfile', '-Command', code], check=True, creationflags=subprocess.CREATE_NO_WINDOW)

def desktop_shortcut(root, desktop_exe):
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    launcher = root / 'native_launcher.py'
    code = (
        '$nativeLinkShell = New-Object -ComObject WScript.Shell; '
        '$nativeDesktop = [Environment]::GetFolderPath("Desktop"); '
        '$nativeLinkPath = Join-Path $nativeDesktop "Codex（原生 DS）.lnk"; '
        'if (Test-Path -LiteralPath $nativeLinkPath) { throw "Shortcut already exists; inspect before replacing" }; '
        '$nativeLink = $nativeLinkShell.CreateShortcut($nativeLinkPath); '
        f'$nativeLink.TargetPath = {ps_quote(pythonw)}; '
        f'$nativeLink.Arguments = {ps_quote(chr(34)+str(launcher)+chr(34)+" open")}; '
        f'$nativeLink.WorkingDirectory = {ps_quote(root)}; $nativeLink.IconLocation = {ps_quote(desktop_exe)}; '
        '$nativeLink.Save(); ConvertTo-Json -Compress -InputObject @($nativeLinkPath)')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', code], capture_output=True, text=True,
                            check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    value = json.loads(result.stdout)
    return [value] if isinstance(value, str) else value

def main():
    parser = argparse.ArgumentParser(description='Install reviewed native DS routing with backups. Requires a full Codex exit/reopen afterward.')
    parser.add_argument('--activate', action='store_true', help='Write the formal config, rules, installed Skill, task and desktop shortcut')
    args = parser.parse_args()
    if not args.activate:
        parser.print_help(); return
    if os.name != 'nt':
        raise RuntimeError('This installation procedure targets Windows')
    home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).resolve()
    if home != (Path.home() / '.codex').resolve():
        raise RuntimeError('Run from a normal terminal, not an isolated CODEX_HOME test environment')
    root = home / 'native-model-adapter'
    root.mkdir(exist_ok=True)
    if (root / 'deployment.json').exists():
        raise RuntimeError('An installation manifest exists. Inspect or rollback the previous deployment first.')
    original = (home / 'config.toml').read_text()
    config = tomllib.loads(original)
    provider = config['model_provider']
    source = config['model_catalog_json']
    upstream = config['model_providers'][provider]['base_url']
    if not isinstance(upstream, str) or not upstream.strip():
        raise RuntimeError('The current provider needs a base_url before installing')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    package = subprocess.run(['powershell.exe', '-NoProfile', '-Command', 'Get-AppxPackage -Name OpenAI.Codex | Select-Object -First 1 -ExpandProperty InstallLocation'], capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    desktop_exe = Path(package.stdout.strip()) / 'app/ChatGPT.exe'
    if not desktop_exe.is_file(): raise RuntimeError('Installed Codex desktop not found')
    token = secrets.token_urlsafe(32)
    settings = {'port': port, 'instance_id': str(uuid.uuid4()), 'parent_base_url': upstream,
                'routes': {'deepseek-flash': {'base_url': 'https://api.deepseek.com', 'key_env': 'DEEPSEEK_API_KEY', 'queue_fallback_model': 'deepseek-v4-pro', 'queue_fallback_seconds': 20, 'route_log': str(root/'route-audit.jsonl'), 'efforts': ['low', 'medium', 'high', 'xhigh'], 'default_effort': 'high'}},
                'token_sha256': hashlib.sha256(token.encode()).hexdigest(), 'state_file': str(root/'call-aliases.json'),
                'source_catalog': source, 'catalog_file': str(root/'models.json'), 'codex_home': str(home),
                'desktop_exe': str(desktop_exe), 'original_provider': provider}
    new_config = render_config(original, provider, settings, token)
    global_rules = home / 'AGENTS.md'
    skill = home / 'skills/external-model-worker/SKILL.md'
    updates = [(home/'config.toml', new_config.encode()), (global_rules, render_rules(global_rules.read_text()).encode()),
               (skill, (PROJECT/'skills/external-model-worker/SKILL.md').read_bytes())]
    backup = root/'backups'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup.mkdir(parents=True)
    files = []
    for index, (target, data) in enumerate(updates):
        backup_file = backup/(str(index)+'-'+target.name)
        shutil.copy2(target, backup_file)
        files.append({'target': str(target), 'backup': str(backup_file), 'installed_sha256': hashlib.sha256(data).hexdigest()})
    stop_owned_service(root)
    for name in ['native_gateway.py', 'native_launcher.py', 'native_install.py']:
        shutil.copy2(PROJECT/name, root/name)
    atomic_write(root/'adapter-token.txt', token.encode())
    try:
        register_service_task(root, settings)
        atomic_write(root/'settings.json', json.dumps(settings, indent=2).encode())
        subprocess.run([sys.executable, str(root/'native_launcher.py'), 'start'], check=True)
    except Exception as error:
        if settings.get('scheduled_task'):
            try:
                stop_owned_service(root)
                delete_service_task(settings['scheduled_task'])
            except Exception as cleanup_error:
                error.add_note('Failed to remove the partial scheduled task: ' + str(cleanup_error))
        raise
    # Commit formal files only after the final build reports healthy.
    written = []
    try:
        for entry, (target, data) in zip(files, updates):
            atomic_write(target, data); written.append(entry)
    except Exception:
        for entry in written:
            atomic_write(entry['target'], Path(entry['backup']).read_bytes())
        raise
    deployment = {'status': 'configured_pending_codex_restart', 'backup_dir': str(backup), 'files': files, 'shortcuts': []}
    atomic_write(root/'deployment.json', json.dumps(deployment, indent=2).encode())
    try:
        deployment['shortcuts'] = desktop_shortcut(root, desktop_exe)
        atomic_write(root/'deployment.json', json.dumps(deployment, indent=2).encode())
    except Exception:
        print('Configuration is installed; shortcut creation failed. Use native_launcher.py open directly; backups remain available.')
        raise
    print('Configured native DeepSeek routing. Backup: '+str(backup))
    print('Fully quit Codex, including the tray process, then use the desktop Codex native DS shortcut.')
    print('Rollback: python "'+str(root/'native_launcher.py')+'" rollback')

if __name__ == '__main__':
    main()

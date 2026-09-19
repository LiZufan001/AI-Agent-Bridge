"""Idempotent daily console launcher; never starts or kills a Worker executor."""
from __future__ import annotations

import state_roots

import argparse
from dashboard_identity import service_instance_id
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
import webbrowser


def _configure_text_output() -> None:
    """Keep human diagnostics printable on legacy Windows code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors='backslashreplace')
        except (OSError, ValueError):
            pass


def _dashboard_child_env() -> dict[str, str]:
    """Use a stable UTF-8 encoding for detached Dashboard service logs."""
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    return env


def probe(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url + '/api/service', timeout=2) as response:
            return json.loads(response.read(4096))
    except (OSError, ValueError):
        return None


def main() -> int:
    _configure_text_output()
    parser = argparse.ArgumentParser(description='Bridge 控制台日常运行管理')
    parser.add_argument('action', choices=['start', 'open', 'status', 'stop'])
    parser.add_argument('--state-root', '--bridge-root', dest='bridge_root', type=Path)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--read-only', action='store_true', help='Default; retained compatibility flag')
    parser.add_argument('--allow-controls', action='store_true', help='Explicitly enable Owner actions')
    parser.add_argument('--logs', type=Path)
    args = parser.parse_args()
    if args.read_only and args.allow_controls:
        parser.error("--read-only and --allow-controls conflict")
    args.read_only = not args.allow_controls
    root = state_roots.resolve_state_root(args.bridge_root, for_write=args.allow_controls)
    expected = service_instance_id(root, create=args.action in {'start', 'open'})
    url = f'http://127.0.0.1:{args.port}'
    running = probe(url)
    if running and (running.get('service') != 'bridge-owner-console' or running.get('root_id') != expected):
        print('此端口属于其他服务或其他 Bridge。请使用另一个端口。')
        return 2
    if args.action == 'status':
        print(json.dumps({'running': bool(running), 'url': url, 'allow_controls': (running or {}).get('allow_controls')}, ensure_ascii=False))
        return 0 if running else 1
    if args.action == 'stop':
        if running:
            request = urllib.request.Request(url + '/api/shutdown', method='POST',
                data=json.dumps({'root_id': expected}).encode(),
                headers={'Content-Type': 'application/json', 'X-Bridge-Owner': '1', 'Origin': url})
            with urllib.request.urlopen(request, timeout=3) as response:
                response.read(4096)
            for _ in range(30):
                if probe(url) is None:
                    print('控制台已停止。')
                    return 0
                time.sleep(.1)
            print('已请求停止，服务尚未退出。')
            return 2
        print('控制台未运行。')
        return 0
    if not running:
        default_logs = (Path(tempfile.gettempdir()) / 'bridge-synthetic-console' / expected
                        if root == state_roots.fixture_root().resolve() else root / 'worker/runtime/dashboard')
        logs = args.logs or default_logs
        logs.mkdir(parents=True, exist_ok=True)
        executable = Path(sys.executable)
        if os.name == 'nt' and executable.with_name('pythonw.exe').is_file():
            executable = executable.with_name('pythonw.exe')
        argv = [str(executable), '-u', str(Path(__file__).with_name('bridge_cli.py')), 'dashboard', '--serve', '--bridge-root', str(root), '--port', str(args.port)]
        if not args.read_only:
            argv.append('--allow-controls')
        options = {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        with (logs / 'service.log').open('ab') as output:
            process = subprocess.Popen(
                argv, cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                env=_dashboard_child_env(), **options
            )
        for _ in range(50):
            running = probe(url)
            if running:
                break
            if process.poll() is not None:
                print('启动失败，请检查端口占用和 service.log。')
                return 2
            time.sleep(.1)
        if not running or running.get('root_id') != expected:
            print('启动未确认，请检查服务日志。')
            return 2
    print(url)
    if running.get('allow_controls') == args.read_only:
        print('现有服务的操作模式不同；如需切换，请先 stop 再 start。')
    if args.action == 'open':
        webbrowser.open(url)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

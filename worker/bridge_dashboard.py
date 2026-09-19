"""Loopback Owner console. Projection is read-only; actions use Owner control APIs."""
from __future__ import annotations

import state_roots

import argparse
from dashboard_identity import service_instance_id
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit, parse_qs

try:
    from .vnext_runtime.health_aggregator import HealthAggregator, HealthSnapshot
except ImportError:
    from vnext_runtime.health_aggregator import HealthAggregator, HealthSnapshot
import console_observation
import owner_console_control as control

ASSETS = Path(__file__).with_name("dashboard_assets")


def render_dashboard(snapshot: HealthSnapshot) -> str:
    """Portable offline view using exactly the same presentation as the live console."""
    data = json.dumps(snapshot.to_dict(), ensure_ascii=False).replace("<", "\\u003c")
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    html = html.replace('<link rel="stylesheet" href="/app.css">', '<style>' + (ASSETS / 'app.css').read_text(encoding='utf-8') + '</style>')
    return html.replace('<script src="/app.js" defer></script>', '<script>window.BRIDGE_STATIC=' + data + ';</script><script defer>' + (ASSETS / 'app.js').read_text(encoding='utf-8') + '</script>')


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, root: Path, *, port: int = 8765, config_path: Path | None = None, allow_controls: bool = False):
        self.bridge_root = root.resolve()
        self.config_path = config_path
        self.allow_controls = allow_controls
        self.action_lock = threading.Lock()
        self.clients = threading.BoundedSemaphore(24)
        self.root_id = service_instance_id(self.bridge_root)
        super().__init__(("127.0.0.1", port), DashboardHandler)

    def process_request(self, request, address):
        if not self.clients.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.clients.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.clients.release()

    def snapshot(self) -> dict:
        value = HealthAggregator(self.bridge_root, config_path=self.config_path).collect().to_dict()
        try:
            value['portfolio_revision'] = control.revision(control.read_object(self.bridge_root / 'supervisor/portfolio.json'))
        except (OSError, ValueError, control.WorkerError):
            value['portfolio_revision'] = None
        worker = value['worker']
        process = worker.get('process', {})
        worker['process_alive'] = console_observation.host_process_alive(process.get('worker_pid') or process.get('pid'))
        if worker['process_alive'] is not True:
            worker['severity'] = 'Attention'
            value['coordinator']['severity'] = 'Attention'
            value['warnings'].append('worker process liveness unconfirmed')
            if value['severity'] == 'Healthy':
                value['severity'] = 'Attention'
        ready = self.allow_controls and worker['process_alive'] is True and worker.get('control_version') == control.VERSION and worker.get('heartbeat', {}).get('fresh') is True
        value['controls'] = {'enabled': ready, 'reason': 'ready' if ready else ('read_only' if not self.allow_controls else 'worker_upgrade_or_freshness_required')}
        for project in value['projects']:
            project['recent_result'] = console_observation.report_metadata(self.bridge_root, project['project_id'], project.get('latest_report'))
            project['task'] = console_observation.command_summary(self.bridge_root, project['project_id'], project.get('latest_command'))
            active = project.get('active_run')
            if active:
                active['process_alive'] = console_observation.process_alive(self.bridge_root, project['project_id'], active['run_id'])
                if active['process_alive'] is not True:
                    project['severity'] = 'Action Required'
                    project['warnings'].append('active run process liveness unconfirmed')
                    value['severity'] = 'Action Required'
        return value


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *_args):
        pass  # URLs and request bodies never enter access logs.

    def _send(self, code: int, body: bytes, content_type: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (OSError, socket.timeout):
            pass

    def _json(self, code: int, value: dict):
        self._send(code, json.dumps(value, ensure_ascii=False).encode('utf-8'))

    def _trusted(self, *, mutation=False):
        expected = f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host') != expected or self.headers.get('Sec-Fetch-Site') in {'cross-site', 'same-site'}:
            self._json(403, {'error': 'local_origin_required'})
            return False
        origin = self.headers.get('Origin')
        if (origin and origin != f'http://{expected}') or (mutation and origin != f'http://{expected}'):
            self._json(403, {'error': 'origin_mismatch'})
            return False
        return True

    def do_GET(self):
        if not self._trusted():
            return
        url = urlsplit(self.path)
        assets = {'/': ('index.html', 'text/html'), '/app.css': ('app.css', 'text/css'), '/app.js': ('app.js', 'text/javascript')}
        try:
            if url.path in assets:
                name, mime = assets[url.path]
                self._send(200, (ASSETS / name).read_bytes(), mime + '; charset=utf-8')
            elif url.path == '/api/health':
                self._json(200, self.server.snapshot())
            elif url.path == '/api/service':
                self._json(200, {'service': 'bridge-owner-console', 'version': 1, 'root_id': self.server.root_id,
                                 'allow_controls': self.server.allow_controls})
            elif url.path == '/api/run':
                query = parse_qs(url.query, max_num_fields=3)
                project, run_id = query.get('project', [''])[0], query.get('run', [''])[0]
                snapshot = self.server.snapshot()
                item = next((p for p in snapshot['projects'] if p['project_id'] == project), None)
                allowed_runs = {((item or {}).get('active_run') or {}).get('run_id'), ((item or {}).get('recent_result') or {}).get('run_id')}
                if not item or not run_id or run_id not in allowed_runs:
                    self._json(404, {'error': 'run_not_available'})
                    return
                self._json(200, console_observation.run_view(self.server.bridge_root, project, run_id))
            else:
                self._json(404, {'error': 'not_found'})
        except (OSError, ValueError, control.WorkerError):
            self._json(503, {'error': 'evidence_unavailable'})

    def do_POST(self):
        if not self._trusted(mutation=True):
            return
        if self.path not in {'/api/control', '/api/shutdown'} or (self.path == '/api/control' and not self.server.allow_controls):
            self._json(405, {'error': 'controls_disabled'})
            return
        if self.headers.get('Content-Type') != 'application/json' or self.headers.get('X-Bridge-Owner') != '1' or self.headers.get('Transfer-Encoding'):
            self._json(415, {'error': 'json_owner_request_required'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                self._json(413, {'error': 'request_too_large'})
                return
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, OSError):
            self._json(400, {'error': 'invalid_request'})
            return
        if self.path == '/api/shutdown':
            if body != {'root_id': self.server.root_id}:
                self._json(409, {'error': 'service_identity_mismatch'})
                return
            if self.server.action_lock.locked():
                self._json(409, {'error': 'another_action_in_progress'})
                return
            self._json(200, {'status': 'stopping'})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if not self.server.action_lock.acquire(blocking=False):
            self._json(409, {'error': 'another_action_in_progress'})
            return
        try:
            snapshot = self.server.snapshot()
            if not snapshot['controls']['enabled']:
                self._json(409, {'error': 'worker_upgrade_or_freshness_required'})
                return
            project = body.get('project_id')
            if not isinstance(project, str) or not any(p['project_id'] == project for p in snapshot['projects']):
                raise ValueError()
            action = body.get('action')
            if action in {'pause', 'resume'} and set(body) == {'project_id', 'action', 'revision'}:
                result = control.set_project_paused(self.server.bridge_root, project, action == 'pause', body['revision'])
            elif action == 'stop' and set(body) == {'project_id', 'action', 'run_id', 'command_id', 'generation'}:
                result = control.request_stop(self.server.bridge_root, project, body)
            else:
                raise ValueError()
            self._json(200, result)
        except (control.WorkerError, OSError):
            self._json(409, {'error': 'state_changed_or_publication_unconfirmed'})
        except (ValueError, TypeError, KeyError):
            self._json(400, {'error': 'invalid_request'})
        finally:
            self.server.action_lock.release()

    def do_PUT(self):
        self._json(405, {'error': 'method_not_allowed'})
    do_PATCH = do_DELETE = do_OPTIONS = do_PUT


def create_server(bridge_root: Path, *, port=8765, config_path=None, allow_controls=False) -> DashboardServer:
    return DashboardServer(bridge_root, port=port, config_path=config_path, allow_controls=allow_controls)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='bridge dashboard', description='本地 Bridge 运维控制台')
    parser.add_argument('--state-root', '--bridge-root', dest='bridge_root', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--serve', action='store_true', help='持续提供本地控制台')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--allow-controls', action='store_true', help='启用受控 Owner 操作')
    parser.add_argument('--output', type=Path, help='导出离线只读快照')
    args = parser.parse_args(argv)
    args.bridge_root = state_roots.resolve_state_root(args.bridge_root, for_write=args.allow_controls)
    if args.serve:
        if args.output:
            parser.error('--serve 与 --output 不能同时使用')
        with create_server(args.bridge_root, port=args.port, config_path=args.config, allow_controls=args.allow_controls) as server:
            print(f'Bridge 控制台 http://127.0.0.1:{server.server_port}', flush=True)
            try:
                server.serve_forever(poll_interval=0.5)
            except KeyboardInterrupt:
                pass
        return 0
    snapshot = HealthAggregator(args.bridge_root, config_path=args.config).collect()
    html = render_dashboard(snapshot)
    if args.output:
        args.output.write_text(html, encoding='utf-8')
    else:
        print(html)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

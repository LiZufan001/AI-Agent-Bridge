"""No external network is used: fixed synthetic failures and local process IO."""
import os
import socket
import ssl
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import git_store
from vnext_runtime.guards.network import NetworkGuard, _safe_diagnostic_reason


class NetworkDiagnosticTests(unittest.TestCase):
    def config(self):
        return {'network_guard': {'enabled': True, 'probe_url': 'https://probe.invalid/',
                                 'fail_closed': True, 'timeout_seconds': 1}}

    def test_classified_failures_remain_closed_without_raw_exception(self):
        cases = [(TimeoutError('synthetic message'), 'probe transport timeout'),
                 (URLError(socket.gaierror(1, 'synthetic message')), 'probe DNS resolution failed'),
                 (URLError(ssl.SSLError('synthetic message')), 'probe TLS handshake failed'),
                 (ConnectionResetError('synthetic message'), 'probe connection failed'),
                 (HTTPError('https://probe.invalid/', 503, 'synthetic message', {}, None), 'probe returned HTTP 503')]
        for error, reason in cases:
            with self.subTest(reason=reason):
                opener = lambda *a, err=error, **k: (_ for _ in ()).throw(err)
                verdict = NetworkGuard(self.config(), opener=opener).preflight()
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.reason, reason)
                self.assertEqual(_safe_diagnostic_reason(reason), reason)
                self.assertNotIn('synthetic message', repr(verdict))

    def test_nonfinite_timeout_is_rejected(self):
        for timeout in (float('nan'), float('inf'), -1, 0):
            config = self.config(); config['network_guard']['timeout_seconds'] = timeout
            self.assertFalse(NetworkGuard(config).preflight().allowed)


class GitTimeoutConfigurationTests(unittest.TestCase):
    def test_network_timeout_is_bounded_and_local_verbs_unchanged(self):
        for invalid in ('nan', 'inf', '-1', '0', '301', 'words'):
            with patch.dict(os.environ, {'BRIDGE_GIT_NETWORK_TIMEOUT_SECONDS': invalid}):
                with self.assertRaises(git_store.GitTransportError):
                    git_store.network_timeout_seconds()
        with patch.dict(os.environ, {'BRIDGE_GIT_NETWORK_TIMEOUT_SECONDS': '2'}):
            self.assertEqual(git_store.network_timeout_seconds(), 2)
        self.assertEqual(git_store._git_verb(('-c', 'x=y', 'fetch', 'origin')), 'fetch')
        self.assertEqual(git_store._git_verb(('status',)), 'status')
        with patch.object(git_store, 'run_process') as local:
            git_store.git(Path('.'), 'status')
            local.assert_called_once_with(['git', 'status'], cwd=Path('.'), check=True)


if __name__ == '__main__':
    unittest.main()

"""Real Git/CLI/child execution integration using only temporary synthetic data."""
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bridge_worker as worker
import bridge_worker_hardened as hardened
import git_store
import supervisor_tools as helpers
from supervisor_publication_gateway import SupervisorPublicationGateway
from test_state_fixture import make_state
from worker_execution_control import ExecutionControlDecision


class PreparedRequestIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-prepared-integration-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.remote = self.root / 'remote.git'
        self.state = self.root / 'state'
        self.product = self.root / 'product'
        self.git(self.root, 'init', '--bare', str(self.remote))
        self.git(self.root, 'clone', str(self.remote), str(self.state))
        make_state(self.state)
        self.identity(self.state)
        self.git(self.state, 'branch', '-M', 'main')
        (self.state / '.gitignore').write_text('worker/runtime/\n', encoding='utf-8')
        project = self.state / 'projects/p'
        for sub in ('commands', 'reports'):
            (project / sub).mkdir(parents=True)
            (project / sub / '.keep').write_text('\n')
        (project / 'MISSION.md').write_text('# Synthetic integration mission\n')
        value = {'protocol_version': 2, 'project_id': 'p', 'status': 'REPORT_READY',
                 'generation': 0, 'latest_command': 0, 'latest_report': 0,
                 'last_reviewed_report': 0, 'active_run': None,
                 'finalized': False, 'human_required': False}
        self.state_path = project / 'state.json'
        self.state_path.write_text(json.dumps(value) + '\n', encoding='utf-8')
        (self.state / 'supervisor/bootstrap.json').write_text(json.dumps({
            'schema_version': 1,
            'staged_publication': {'request_directory': 'worker/staged-publications/requests',
                                   'max_lifetime_seconds': 600, 'max_future_skew_seconds': 60}
        }), encoding='utf-8')
        (self.state / 'supervisor/portfolio.json').write_text(json.dumps({
            'schema_version': 1, 'projects': [{'project_id': 'p', 'priority_rank': 10,
                'owner_selected': True, 'owner_paused': False}]}), encoding='utf-8')
        self.git(self.state, 'add', '.')
        self.git(self.state, 'commit', '-m', 'synthetic initial State')
        self.git(self.state, 'push', '-u', 'origin', 'main')
        self.product.mkdir()
        self.git(self.product, 'init')
        self.identity(self.product)
        fake = self.product / 'fake_executor.py'
        fake.write_text(
            'import json,sys\nfrom pathlib import Path\n'
            'sys.stdin.read()\n'
            'count=Path("invocations.txt")\n'
            'count.write_text(str(int(count.read_text())+1) if count.exists() else "1")\n'
            'out=Path(sys.argv[sys.argv.index("--output-last-message")+1])\n'
            'out.write_text("Synthetic execution completed.\\nBRIDGE_EXECUTION_JSON: "'
            '+json.dumps({"status":"SUCCESS"})+"\\n",encoding="utf-8")\n', encoding='utf-8')
        self.git(self.product, 'add', 'fake_executor.py')
        self.git(self.product, 'commit', '-m', 'synthetic executor fixture')
        self.fake = fake
        control = patch('worker_execution_control.read_execution_control', return_value=
            ExecutionControlDecision('auto', 'owner_resume', 'owner',
                                     '2020-01-01T00:00:00Z', True, 'fixture'))
        control.start(); self.addCleanup(control.stop)

    def git(self, root, *args):
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                                text=True, encoding='utf-8', timeout=15)
        if result.returncode:
            self.fail('Synthetic Git operation failed: ' + result.stderr[-1500:])
        return result.stdout.strip()

    def identity(self, root):
        self.git(root, 'config', 'user.name', 'Synthetic Test')
        self.git(root, 'config', 'user.email', 'fixture@example.test')

    def stage(self, prepared, head):
        receipt = helpers.write_staged(self.state, prepared, expected_head=head,
                                      producer_attestation='scheduled_chatgpt')
        self.assertEqual(receipt['status'], 'STAGED_LOCAL_ONLY')
        relative = receipt['relative_path']
        self.git(self.state, '-c', 'core.autocrlf=false', 'add', '--', relative)
        self.git(self.state, 'commit', '-m', 'synthetic staged input')
        self.git(self.state, 'push', 'origin', 'HEAD:main')
        return self.state / relative

    def test_prepare_gateway_real_claim_child_report_and_no_second_execution(self):
        before = self.state_path.read_bytes()
        prepared, head = helpers.prepare_from_repository(self.state, 'p',
            '# Task\r\nRun the synthetic fixture once. 验证🙂\r\n',
            producer='scheduled_chatgpt', request_id='whole-chain')
        self.assertEqual(self.state_path.read_bytes(), before)
        self.stage(prepared, head)
        gateway = SupervisorPublicationGateway(self.state)
        first = gateway.poll()
        self.assertEqual(first[0].outcome, 'published')
        command = self.state / 'projects/p/commands/command-001.md'
        self.assertEqual(git_store.read_blob(self.state, 'projects/p/commands/command-001.md'), prepared.command_bytes)
        config = {'codex_command': sys.executable, 'codex_execution_mode': 'full_access',
                  'codex_args': [str(self.fake), 'exec', '--dangerously-bypass-approvals-and-sandbox'],
                  'codex_timeout_seconds': 20, 'network_guard': {'enabled': False}}
        with patch.object(worker, 'codex_run', side_effect=hardened.hardened_codex_run):
            self.assertTrue(worker.process_project(self.state, 'p', {'workdir': str(self.product)}, config))
            self.assertFalse(worker.process_project(self.state, 'p', {'workdir': str(self.product)}, config))
        final = json.loads(self.state_path.read_bytes())
        self.assertEqual(final['status'], 'REPORT_READY')
        self.assertEqual(final['generation'], 3)
        self.assertEqual(final['latest_report'], 1)
        self.assertIsNone(final['active_run'])
        self.assertEqual((self.product / 'invocations.txt').read_text(), '1')
        remote_state = json.loads(self.git(self.remote, 'show', 'main:projects/p/state.json'))
        self.assertEqual(remote_state, final)
        self.assertIn('SUCCESS', self.git(self.remote, 'show', 'main:projects/p/reports/report-001.md'))
        self.assertEqual(gateway.poll(), ())

    def test_create_only_stale_and_forged_bytes_are_refused(self):
        prepared, head = helpers.prepare_from_repository(self.state, 'p', 'fixture', producer='scheduled_chatgpt')
        forged = replace(prepared, command_bytes=b'different bytes')
        with self.assertRaises(helpers.PreparationError):
            helpers.write_staged(self.state, forged, expected_head=head, producer_attestation='scheduled_chatgpt')
        path = self.stage(prepared, head)
        saved = path.read_bytes()
        with self.assertRaises(helpers.PreparationError):
            helpers.write_staged(self.state, prepared, expected_head=head, producer_attestation='scheduled_chatgpt')
        self.assertEqual(path.read_bytes(), saved)
        expired = helpers.prepare_request(self.state_path.read_bytes(), 'fixture', producer='scheduled_chatgpt',
            now=datetime.now(timezone.utc)-timedelta(hours=1))
        with self.assertRaises(helpers.PreparationError):
            helpers.write_staged(self.state, expired,
                expected_head=self.git(self.state, 'rev-parse', 'HEAD'), producer_attestation='scheduled_chatgpt')

    def test_lost_push_ack_preserves_input_and_reconciles_without_reexecution(self):
        prepared, head = helpers.prepare_from_repository(self.state, 'p', 'fixture',
            producer='scheduled_chatgpt', request_id='lost-ack')
        path = self.stage(prepared, head)
        original = git_store.git
        pushes = []
        def lose_one_ack(root, *args, **kwargs):
            result = original(root, *args, **kwargs)
            if args and args[0] == 'push' and not pushes:
                pushes.append(1)
                raise git_store.GitTransportError('synthetic_lost_ack_after_remote_acceptance')
            return result
        gateway = SupervisorPublicationGateway(self.state)
        with patch.object(git_store, 'git', side_effect=lose_one_ack):
            result = gateway.poll()
        self.assertEqual(result[0].outcome, 'error_safe')
        self.assertTrue(path.exists())
        self.assertEqual(len(pushes), 1)
        self.assertEqual(gateway.poll()[0].outcome, 'already_applied')
        self.assertFalse((self.product / 'invocations.txt').exists())
        self.assertEqual(json.loads(self.state_path.read_bytes())['generation'], 1)

    def test_real_cli_preview_and_context_leave_git_identity_unchanged(self):
        body = self.product / 'body.md'; body.write_text('Synthetic body. 中文🙂', encoding='utf-8')
        head = self.git(self.state, 'rev-parse', 'HEAD')
        cli = Path(helpers.__file__)
        result = subprocess.run([sys.executable, '-B', str(cli), 'prepare', '--state-root', str(self.state),
            '--project', 'p', '--body-file', str(body), '--producer', 'scheduled_chatgpt'],
            capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'PREPARED_NOT_SUBMITTED')
        view = helpers.compact_context(self.state, 'p')
        self.assertEqual(view['state_commit'], head)
        self.assertFalse(view['execution_authorized'])
        self.assertEqual(view['projects'][0]['latest_report_path'], None)
        self.assertEqual(self.git(self.state, 'rev-parse', 'HEAD'), head)
        self.assertFalse((self.state/'worker/staged-publications/requests').exists())


if __name__ == '__main__':
    unittest.main()

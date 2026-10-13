"""Bounded global context and immutable evidence, never future-action authority."""
import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import executor
import git_store
import phased_task
import supervisor_tools
from test_executor import phased_command


class GlobalPhaseContextTests(unittest.TestCase):
    def test_overview_exposes_dependency_not_future_body(self):
        raw = phased_command().replace(b'phase B work', b'Phase B integration\n' + b'FUTURE_DETAIL ' * 3000)
        task = phased_task.parse_phased_task(raw.decode('utf-8'))
        overview = executor.phased_overview(task)
        data = json.loads(overview[overview.index('{'):])
        self.assertEqual(data['phase_dependencies'][1]['after'], 'A')
        self.assertEqual(data['phase_dependencies'][1]['summary'], 'Phase B integration')
        self.assertNotIn('FUTURE_DETAIL', overview)
        self.assertEqual(data['snapshot_sha256'], sha256(task.source_text.encode()).hexdigest())
        self.assertEqual(data['purpose'], 'read_only_global_context_not_advance_authority')
        self.assertLess(len(overview), 4096)
        self.assertEqual(data['final_acceptance_excerpt'], 'final checks')

    def test_future_context_does_not_change_current_projection_or_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = phased_command().replace(b'phase B work', b'Future-stage overview only\nDo not execute here.')
            runtime = phased_task.prepare_runtime(root/'worker'/'runtime'/'p'/'runs'/'run-014-synthetic', raw,
                phased_task.ExecutionIdentity('p', 14, 'run-014-synthetic', 41), bridge_root=root)
            before = runtime.paths.current_phase.read_bytes()
            progress = runtime.paths.progress.read_bytes()
            text = executor.build_phased_prompt('p', 'Synthetic mission', runtime, bridge_root=root)
            self.assertIn('READ-ONLY CONTEXT, NOT permission', text)
            self.assertIn('bypass a checkpoint', text)
            self.assertIn('Complete only the current phase', text)
            self.assertEqual(runtime.paths.current_phase.read_bytes(), before)
            self.assertEqual(runtime.paths.progress.read_bytes(), progress)


class ImmutableContextTests(unittest.TestCase):
    def setUp(self):
        from test_prepared_request_integration import PreparedRequestIntegrationTests
        self.fixture = PreparedRequestIntegrationTests('test_real_cli_preview_and_context_leave_git_identity_unchanged')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_focus_excerpt_is_bounded_bound_and_non_authoritative(self):
        f = self.fixture
        goal = f.state/'projects/p/CURRENT_GOAL.md'
        goal.write_text('# Goal\n' + 'Synthetic detail\n' * 4000, encoding='utf-8')
        f.git(f.state, 'add', 'projects/p/CURRENT_GOAL.md')
        f.git(f.state, 'commit', '-m', 'synthetic goal context')
        head = f.git(f.state, 'rev-parse', 'HEAD')
        result = supervisor_tools.compact_context(f.state, 'p')
        row = result['projects'][0]
        source = row['focus_sources'][0]
        self.assertEqual(source['state_commit'], head)
        self.assertEqual(len(source['excerpt']), 2048)
        self.assertTrue(source['truncated'])
        self.assertFalse(result['execution_authorized'])
        self.assertFalse(row['deep_review_performed'])
        self.assertFalse(row['needs_report_review'])
        self.assertEqual(f.git(f.state, 'rev-parse', 'HEAD'), head)
        self.assertNotIn('focus_sources', supervisor_tools.compact_context(f.state)['projects'][0])

    def test_snapshot_reads_have_explicit_revision_and_detect_concurrent_change(self):
        f = self.fixture
        original = git_store.read_blob
        revisions = []
        def observe(*args, **kwargs):
            revisions.append(kwargs.get('revision'))
            return original(*args, **kwargs)
        with patch.object(git_store, 'read_blob', side_effect=observe):
            result = supervisor_tools.compact_context(f.state, 'p')
        self.assertTrue(revisions)
        self.assertEqual(set(revisions), {result['state_commit']})
        with patch.object(git_store, 'get_head', side_effect=[result['state_commit'], '1' * 40]):
            with self.assertRaises(supervisor_tools.PreparationError):
                supervisor_tools.compact_context(f.state, 'p')

    def test_immutable_revision_rejects_arbitrary_git_syntax(self):
        for value in ('main', 'HEAD~1', '--help', 'A:other', None):
            with self.subTest(value=value), self.assertRaises(git_store.WorkerError):
                git_store.read_blob(self.fixture.state, 'projects/p/state.json', revision=value)

    def test_recovery_preemption_is_distinct_from_ordinary_eligibility(self):
        projects = [{'project_id': 'a', 'priority_rank': 1, 'owner_selected': True, 'owner_paused': False},
                    {'project_id': 'b', 'priority_rank': 2, 'owner_selected': True, 'owner_paused': False}]
        result = supervisor_tools.choose_focus(projects, ['a'], hour_bucket=0,
            preempt_project='b', preemption_reason='fresh_recovery')
        self.assertEqual(result['focus_project'], 'b')
        self.assertEqual(result['mode'], 'caller_justified_preemption')
        projects[1]['owner_paused'] = True
        with self.assertRaises(supervisor_tools.PreparationError):
            supervisor_tools.choose_focus(projects, ['a'], hour_bucket=0,
                preempt_project='b', preemption_reason='fresh_recovery')


if __name__ == '__main__':
    unittest.main()

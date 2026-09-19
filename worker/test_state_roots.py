"""Synthetic positive/negative tests for the sole new root boundary."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import state_roots as roots
import worker_execution_control as control
import worker_health
from test_state_fixture import make_state

class StateRootTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.state=make_state(self.base/'state')
        self.env=patch.dict(os.environ,{roots.STATE_ENV:''},clear=False);self.env.start();self.addCleanup(self.env.stop)

    def _hermetic_engine(self, *, sibling=False):
        engine=self.base/'engine'
        fixture=make_state(engine/'tests'/'fixtures'/'synthetic-state',git=False)
        sibling_root=engine.with_name(engine.name+'-State')
        if sibling:
            make_state(sibling_root,git=False)
        return engine,fixture,sibling_root

    def test_default_read_is_bundled_synthetic(self):
        engine,fixture,_=self._hermetic_engine()
        with patch.object(roots,'engine_root',return_value=engine):
            self.assertEqual(roots.resolve_state_root(environ={}),fixture.resolve())
    def test_default_read_selects_marked_sibling(self):
        engine,_,sibling=self._hermetic_engine(sibling=True)
        with patch.object(roots,'engine_root',return_value=engine):
            self.assertEqual(roots.resolve_state_root(environ={}),sibling.resolve())
    def test_write_never_falls_back(self):
        with self.assertRaises(roots.StateRootError):roots.resolve_state_root(for_write=True,environ={})
    def test_explicit_overrides_environment(self):
        self.assertEqual(roots.resolve_state_root(self.state,for_write=True,environ={roots.STATE_ENV:'missing'}),self.state)
    def test_environment_write_uses_independent_git(self):
        self.assertEqual(roots.resolve_state_root(for_write=True,environ={roots.STATE_ENV:str(self.state)}),self.state)
    def test_no_git_refuses_write(self):
        shutil.rmtree(self.state/'.git')
        with self.assertRaises(roots.StateRootError):roots.resolve_state_root(self.state,for_write=True)
    def test_linked_worktree_refused(self):
        shutil.rmtree(self.state/'.git');(self.state/'.git').write_text('gitdir: elsewhere\n')
        with self.assertRaises(roots.StateRootError):roots.resolve_state_root(self.state,for_write=True)
    def test_missing_marker_not_legacy_fallback(self):
        (self.state/roots.MARKER).unlink()
        with self.assertRaises(roots.StateRootError):roots.resolve_state_root(self.state)
    def test_bad_marker_duplicate_and_boolean_versions(self):
        for raw in ('{"schema_version":1,"schema_version":1}',json.dumps({'schema_version':True,'protocol_version':2,
                     'kind':'synthetic-state','runtime_relative':'worker/runtime'})):
            (self.state/roots.MARKER).write_text(raw)
            with self.assertRaises(roots.StateRootError):roots.resolve_state_root(self.state)
    def test_relative_root_refused(self):
        with self.assertRaises(roots.StateRootError):roots.resolve_state_root('relative')
    def test_fixture_is_not_writable(self):
        with self.assertRaises(roots.StateRootError):roots.resolve_state_root(roots.fixture_root(),for_write=True)
    def test_engine_overlap_refused(self):
        with patch.object(roots,'engine_root',return_value=self.state):
            with self.assertRaises(roots.StateRootError):roots.resolve_state_root(self.state)
    def test_symlink_refused(self):
        link=self.base/'link'
        try:link.symlink_to(self.state,target_is_directory=True)
        except OSError:self.skipTest('host cannot create test symlink')
        with self.assertRaises(roots.StateRootError):roots.resolve_state_root(link)
    def test_adoption_and_live_workdir_blocked(self):
        with self.assertRaises(roots.StateRootError):roots.require_split_runtime_policy(self.state,{'adoption':{'controlled_adoption_enabled':True}})
        with self.assertRaises(roots.StateRootError):roots.guard_execution_workdir(self.state,self.state,{})
        with self.assertRaises(roots.StateRootError):roots.guard_execution_workdir(self.state,roots.engine_root(),{})
        roots.guard_execution_workdir(self.state,self.base/'product',{})
        with self.assertRaises(roots.StateRootError):roots.guard_execution_workdir(self.state,self.base/'maintenance',{'self_maintenance':{'enabled':True}})
    def _private(self):
        marker=json.loads((self.state/roots.MARKER).read_text());marker['kind']='private-state'
        (self.state/roots.MARKER).write_text(json.dumps(marker))
        bootstrap=json.loads((roots.fixture_root()/'supervisor/bootstrap.json').read_text())
        (self.state/'supervisor/bootstrap.json').write_text(json.dumps(bootstrap))
        return bootstrap
    def test_private_missing_binding_fails_closed_before_network(self):
        self._private()
        with patch.object(control,'_github_token') as token:
            self.assertFalse(control.read_execution_control(self.state).execution_allowed)
            token.assert_not_called()
    def test_private_control_binding_exact_match_and_tamper(self):
        bootstrap=self._private();(self.state/'worker').mkdir(exist_ok=True)
        binding={'schema_version':1,'execution_control':bootstrap['execution_control'],
                 'heartbeat':{'repository':bootstrap.get('heartbeat_repository',bootstrap['repository']),**bootstrap['heartbeat']}}
        (self.state/roots.BINDING).write_text(json.dumps(binding))
        roots.require_split_runtime_policy(self.state,{'adoption':{}})
        self.assertEqual(control._bootstrap_contract(self.state),bootstrap['execution_control'])
        bootstrap['execution_control']['repository']='other-owner/other-control'
        (self.state/'supervisor/bootstrap.json').write_text(json.dumps(bootstrap))
        with self.assertRaises(control.ExecutionControlError):control._bootstrap_contract(self.state)
    def test_heartbeat_uses_pinned_separate_repository(self):
        bootstrap=self._private();bootstrap['heartbeat_repository']='example-owner/external-heartbeat'
        (self.state/'supervisor/bootstrap.json').write_text(json.dumps(bootstrap));(self.state/'worker').mkdir(exist_ok=True)
        binding={'schema_version':1,'execution_control':bootstrap['execution_control'],
                 'heartbeat':{'repository':bootstrap['heartbeat_repository'],**bootstrap['heartbeat']}}
        (self.state/roots.BINDING).write_text(json.dumps(binding))
        value=worker_health._read_external_heartbeat_contract(self.state)
        self.assertIsNotNone(value);self.assertEqual(value['repository'],bootstrap['heartbeat_repository'])
        bootstrap['heartbeat']['comment_id']+=1
        (self.state/'supervisor/bootstrap.json').write_text(json.dumps(bootstrap))
        self.assertIsNone(worker_health._read_external_heartbeat_contract(self.state))

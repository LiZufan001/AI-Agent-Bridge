"""Real local Git+HTTP tests. No credentials, network services, or Codex products."""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import state_roots
import git_store
import bridge_common
from test_state_fixture import make_state
sys.path.insert(0,str(state_roots.engine_root()/'tools'))
import sidecar_smoke
import validate_state
import schema_check


def git(root,*args):
    return subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True,check=True).stdout.strip()

class SplitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
    def test_real_git_cas_moves_only_state_and_stale_cas_refused(self):
        engine=self.base/'engine';engine.mkdir();git(engine,'init','-q')
        git(engine,'config','user.name','Synthetic Engine');git(engine,'config','user.email','engine@example.test')
        (engine/'README.md').write_text('synthetic source\n');git(engine,'add','.');git(engine,'commit','-qm','source')
        engine_head=git(engine,'rev-parse','HEAD');engine_tree=sidecar_smoke.hashes(engine)
        remote=self.base/'state-remote.git';git(self.base,'init','--bare','-q',str(remote))
        state=self.base/'state';git(self.base,'clone','-q',str(remote),str(state));make_state(state)
        git(state,'config','user.name','Synthetic State');git(state,'config','user.email','state@example.test')
        project=state/'projects/example';project.mkdir();path=project/'state.json'
        value={'protocol_version':2,'project_id':'example','status':'REPORT_READY','generation':6,
               'latest_command':2,'latest_report':2,'last_reviewed_report':2,'active_run':None}
        path.write_text(json.dumps(value));git(state,'add','.');git(state,'commit','-qm','state');git(state,'branch','-M','main');git(state,'push','-qu','origin','main')
        with patch.object(state_roots,'engine_root',return_value=engine):
            resolved=state_roots.resolve_state_root(state,for_write=True)
            # Derive the persistence target from the canonical State root. On
            # Windows, tempfile paths may have an 8.3 alias while resolve()
            # returns the long spelling; mixing those representations would
            # trip the store's deliberate lexical-containment check.
            path=resolved/'projects/example/state.json'
            result=git_store.publish_cas(bridge_root=resolved,state_path=path,
                expected=lambda current:current['generation']==6,already_applied=lambda current:False,
                payload_builder=lambda current:{path:bridge_common.json_text({**current,'generation':7})},message='synthetic CAS')
            self.assertEqual(result['generation'],7)
            with self.assertRaises(bridge_common.CASConflict):
                git_store.publish_cas(bridge_root=resolved,state_path=path,
                    expected=lambda current:current['generation']==6,already_applied=lambda current:False,
                    payload_builder=lambda current:{path:'should never write'},message='stale CAS')
        self.assertEqual(json.loads(path.read_text())['generation'],7)
        self.assertEqual(engine_head,git(engine,'rev-parse','HEAD'));self.assertEqual(engine_tree,sidecar_smoke.hashes(engine))
    def test_independent_state_dashboard_reads_real_http_without_mutation(self):
        state=self.base/'state';shutil.copytree(state_roots.fixture_root(),state)
        result=sidecar_smoke.smoke(state)
        self.assertTrue(result['pass'],result);self.assertTrue(result['service_root_matches'])
    def test_synthetic_state_full_validation_and_invalid_goal(self):
        state=self.base/'state';shutil.copytree(state_roots.fixture_root(),state)
        result=validate_state.validate_state(state)
        self.assertTrue(result['pass'],result['errors'])
        (state/'projects/engine-maintenance/CURRENT_GOAL.md').write_text('invalid pointer\n')
        self.assertFalse(validate_state.validate_state(state)['pass'])
    def test_exact_operator_archive_review_preserves_history_and_fails_on_drift(self):
        state=self.base/'state';shutil.copytree(state_roots.fixture_root(),state)
        archive=state/'migration/reviewed-evidence.tar.xz'
        archive.parent.mkdir();archive.write_bytes(b'\xfd7zXZ\x00synthetic reviewed archive')
        policy=self.base/'operator-review.json'
        entry={'path':'migration/reviewed-evidence.tar.xz','line':0,
               'rule':'binary-unreviewed-file','file_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
               'reason':'Synthetic immutable archive independently reviewed by operator.'}
        policy.write_text(json.dumps([entry]))
        before=archive.read_bytes()
        self.assertFalse(validate_state.validate_state(state)['pass'])
        with patch.object(validate_state.privacy_scan,'_read_json',
                          wraps=validate_state.privacy_scan._read_json) as read_policy:
            accepted=validate_state.validate_state(state,archive_review=policy)
            self.assertEqual(read_policy.call_count,1)
        self.assertTrue(accepted['pass'],accepted['errors'])
        self.assertEqual(archive.read_bytes(),before)
        self.assertEqual(len(accepted['privacy']['explained_findings']),1)
        archive.write_bytes(before+b'changed')
        self.assertFalse(validate_state.validate_state(state,archive_review=policy)['pass'])
    def test_archive_review_cannot_exempt_credentials_or_non_migration_paths(self):
        state=self.base/'state';shutil.copytree(state_roots.fixture_root(),state)
        policy=self.base/'operator-review.json'
        for path,rule in [('migration/reviewed.bin','credential-value'),
                          ('projects/reviewed.bin','binary-unreviewed-file'),
                          ('migration/../projects/reviewed.bin','binary-unreviewed-file')]:
            with self.subTest(path=path,rule=rule):
                policy.write_text(json.dumps([{'path':path,'line':0,'rule':rule,
                    'file_sha256':'0'*64,'reason':'Synthetic invalid exception.'}]))
                self.assertFalse(validate_state.validate_state(state,archive_review=policy)['pass'])
    def _add_terminal_archive_with_blocker_correction(self, state: Path) -> str:
        pid='historical-release'
        project=state/'projects'/pid
        (project/'owner-actions').mkdir(parents=True)
        value={'protocol_version':2,'project_id':pid,'status':'DONE','generation':0,
               'latest_command':0,'latest_report':0,'last_reviewed_report':0,
               'active_run':None,'finalized':True,'human_required':False}
        (project/'state.json').write_text(json.dumps(value)+'\n')
        (project/'MISSION.md').write_text('# Historical synthetic mission\n')
        (project/'owner-actions/owner-action-001.md').write_text(
            '- action_id: owner-action-001\n- root_action_id: owner-action-001\n' +
            '- relates_to: null\n- blocker_key: original-blocker\n'
            '- owner_status: OWNER_REPORTED_DONE\n- verification_status: VERIFIED\n')
        (project/'owner-actions/owner-action-002.md').write_text(
            '- action_id: owner-action-002\n- root_action_id: owner-action-001\n'
            '- relates_to: owner-action-001\n- blocker_key: target-correction\n'
            '- owner_status: OWNER_REPORTED_DONE\n- verification_status: VERIFIED\n')
        registry=json.loads((state/'worker/remote-projects.json').read_text())
        host=next(iter(registry['hosts'].values()))
        host['projects'][pid]={'enabled':False,'repository':'example-owner/example-archive',
                               'workdir':'X:/synthetic/products/historical-release'}
        (state/'worker/remote-projects.json').write_text(json.dumps(registry,indent=2)+'\n')
        return pid

    def test_terminal_disabled_unselected_project_uses_archival_owner_validation(self):
        state=self.base/'state';shutil.copytree(state_roots.fixture_root(),state)
        pid=self._add_terminal_archive_with_blocker_correction(state)
        operational_error=None
        try:
            project_view = __import__('project_view')
            project_view.project_view(state,pid)
        except RuntimeError as exc:
            operational_error=str(exc)
        self.assertEqual(operational_error,'OWNER_BLOCKER_MISMATCH')
        result=validate_state.validate_state(state)
        self.assertTrue(result['pass'],result['errors'])
        row=next(item for item in result['projects'] if item['project_id']==pid)
        self.assertEqual(row['validation_tier'],'archival')

    def test_terminal_project_reselected_returns_to_operational_validation(self):
        state=self.base/'state';shutil.copytree(state_roots.fixture_root(),state)
        pid=self._add_terminal_archive_with_blocker_correction(state)
        portfolio=json.loads((state/'supervisor/portfolio.json').read_text())
        portfolio['projects'].append({'project_id':pid,'priority_rank':50,
            'owner_selected':True,'owner_paused':False})
        (state/'supervisor/portfolio.json').write_text(json.dumps(portfolio,indent=2)+'\n')
        result=validate_state.validate_state(state)
        self.assertFalse(result['pass'])
        self.assertTrue(any('OWNER_BLOCKER_MISMATCH' in e for e in result['errors']),result['errors'])

    def test_terminal_project_reenabled_returns_to_operational_validation(self):
        state=self.base/'state';shutil.copytree(state_roots.fixture_root(),state)
        pid=self._add_terminal_archive_with_blocker_correction(state)
        registry=json.loads((state/'worker/remote-projects.json').read_text())
        host=next(iter(registry['hosts'].values()));host['projects'][pid]['enabled']=True
        (state/'worker/remote-projects.json').write_text(json.dumps(registry,indent=2)+'\n')
        result=validate_state.validate_state(state)
        self.assertFalse(result['pass'])
        self.assertTrue(any('OWNER_BLOCKER_MISMATCH' in e for e in result['errors']),result['errors'])

    def test_schema_boolean_integer_and_unknown_keyword_refused(self):
        self.assertTrue(schema_check.validate(True,{'type':'integer'}))
        self.assertTrue(schema_check.validate({}, {'futureSecurityKeyword':True}))
        with self.assertRaises(ValueError):schema_check.strict_json('{"x":1,"x":2}')
        with self.assertRaises(ValueError):schema_check.strict_json('{"x":NaN}')
    def test_unsettled_request_does_not_become_canonical(self):
        state=self.base/'state';shutil.copytree(state_roots.fixture_root(),state)
        before=validate_state.canonical_hashes(state)
        inbox=state/'worker/staged-publications/requests';inbox.mkdir(parents=True,exist_ok=True)
        (inbox/'request-invalid.json').write_text('{}')
        result=validate_state.validate_state(state,require_quiescent=True,include_runtime=True)
        self.assertFalse(result['pass']);self.assertEqual(before,validate_state.canonical_hashes(state))

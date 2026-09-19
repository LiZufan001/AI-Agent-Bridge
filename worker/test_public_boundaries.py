"""Synthetic privacy-boundary regression tests; no owner or production data."""
from __future__ import annotations
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import worker_health as health
import worker_execution_control as control
import bridge_dashboard as dashboard
import executor

class PublicCredentialTests(unittest.TestCase):
    def test_ambient_tokens_and_gcm_are_not_implicit(self):
        for module in (health, control):
            with self.subTest(module=module.__name__), patch.dict(os.environ, {'GH_TOKEN':'synthetic-generic','GITHUB_TOKEN':'synthetic-ci'}, clear=True), patch.object(module, '_credential_token_from_git', return_value='synthetic-gcm') as lookup:
                self.assertIsNone(module._github_token())
                lookup.assert_not_called()
    def test_scoped_token_is_used_without_gcm(self):
        for module in (health, control):
            with self.subTest(module=module.__name__), patch.dict(os.environ, {'BRIDGE_GITHUB_TOKEN':'synthetic-scoped'}, clear=True), patch.object(module, '_credential_token_from_git') as lookup:
                self.assertEqual(module._github_token(), 'synthetic-scoped')
                lookup.assert_not_called()
    def test_invalid_scoped_token_never_falls_back(self):
        for module in (health, control):
            for bad in ('', 'synthetic\nother', 'x'*4097):
                with self.subTest(module=module.__name__,length=len(bad)), patch.dict(os.environ, {'BRIDGE_GITHUB_TOKEN':bad, 'BRIDGE_ALLOW_GIT_CREDENTIAL_MANAGER':'1'}, clear=True), patch.object(module, '_credential_token_from_git',return_value='synthetic-gcm') as lookup:
                    self.assertIsNone(module._github_token())
                    lookup.assert_not_called()
    def test_gcm_requires_exact_opt_in(self):
        for module in (health, control):
            for setting,allowed in (('1',True),('true',False),('0',False),('',False)):
                with self.subTest(module=module.__name__,setting=setting),patch.dict(os.environ,{'BRIDGE_ALLOW_GIT_CREDENTIAL_MANAGER':setting},clear=True),patch.object(module,'_credential_token_from_git',return_value='synthetic-gcm') as lookup:
                    self.assertEqual(module._github_token(),'synthetic-gcm' if allowed else None)
                    self.assertEqual(lookup.call_count,int(allowed))
    def test_explicit_control_token_does_not_fall_back(self):
        with patch.dict(os.environ,{'BRIDGE_GITHUB_TOKEN':'synthetic-other'},clear=True):
            self.assertIsNone(control._github_token(''))
            self.assertEqual(control._github_token('synthetic-explicit'),'synthetic-explicit')

class PublicHeartbeatTests(unittest.TestCase):
    def contract(self):
        return {'repository':'example-owner/example-state','worker_id':'worker-public-alias','issue_number':1,'comment_id':2,'heartbeat_interval_seconds':120,'ttl_seconds':300}
    def setUp(self):
        health._EXTERNAL_HEARTBEAT_LAST_SUCCESS_MONOTONIC=0.0
        health._EXTERNAL_HEARTBEAT_LAST_ATTEMPT_MONOTONIC=0.0
    def test_external_alias_is_independent_of_local_host(self):
        with patch.dict(os.environ,{'BRIDGE_LOCAL_WORKER_HOST':'HOST-PRIVATE-SYNTHETIC'},clear=True),patch.object(health.platform,'node',return_value='HOST-PRIVATE-SYNTHETIC'),patch.object(health,'_read_external_heartbeat_contract',return_value=self.contract()),patch.object(health,'_patch_external_heartbeat',return_value=True) as publish:
            health._maybe_publish_external_heartbeat(Path('unused'),{'coordinator_lifecycle':'RUNNING'})
            publish.assert_called_once()
            self.assertNotIn('HOST-PRIVATE-SYNTHETIC',json.dumps(publish.call_args.args[0]))
    def test_missing_local_binding_does_not_publish_hostname(self):
        c=self.contract();c['worker_id']='HOST-PRIVATE-SYNTHETIC'
        with patch.dict(os.environ,{},clear=True),patch.object(health.platform,'node',return_value='HOST-PRIVATE-SYNTHETIC'),patch.object(health,'_read_external_heartbeat_contract',return_value=c),patch.object(health,'_patch_external_heartbeat',return_value=True) as publish:
            health._maybe_publish_external_heartbeat(Path('unused'),{'coordinator_lifecycle':'RUNNING'})
            publish.assert_not_called()
    def test_wrong_local_binding_rejected(self):
        with patch.dict(os.environ,{'BRIDGE_LOCAL_WORKER_HOST':'OTHER-SYNTHETIC'},clear=True),patch.object(health.platform,'node',return_value='HOST-PRIVATE-SYNTHETIC'),patch.object(health,'_read_external_heartbeat_contract',return_value=self.contract()),patch.object(health,'_patch_external_heartbeat',return_value=True) as publish:
            health._maybe_publish_external_heartbeat(Path('unused'),{'coordinator_lifecycle':'RUNNING'})
            publish.assert_not_called()
    def test_configured_hostname_as_alias_rejected(self):
        c=self.contract();c['worker_id']='host-private-synthetic'
        with patch.dict(os.environ,{'BRIDGE_LOCAL_WORKER_HOST':'HOST-PRIVATE-SYNTHETIC'},clear=True),patch.object(health.platform,'node',return_value='HOST-PRIVATE-SYNTHETIC'),patch.object(health,'_read_external_heartbeat_contract',return_value=c),patch.object(health,'_patch_external_heartbeat',return_value=True) as publish:
            health._maybe_publish_external_heartbeat(Path('unused'),{'coordinator_lifecycle':'RUNNING'})
            publish.assert_not_called()

class PublicDashboardTests(unittest.TestCase):
    def test_http_identity_is_not_a_path_digest(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/'private-state-name';root.mkdir()
            server=dashboard.create_server(root,port=0)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                conn.request('GET','/api/service');resp=conn.getresponse();raw=resp.read();conn.close()
                self.assertEqual(resp.status,200)
                payload=json.loads(raw);expected=hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()
                self.assertNotEqual(payload['root_id'],expected)
                self.assertNotIn(str(root).encode(),raw)
                self.assertEqual(payload['root_id'],server.root_id)
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                conn.request('POST','/api/shutdown',json.dumps({'root_id':'wrong-id'}),{'Content-Type':'application/json','X-Bridge-Owner':'1','Origin':f'http://127.0.0.1:{server.server_port}'})
                resp=conn.getresponse();self.assertEqual(resp.status,409);resp.read();conn.close()
            finally:
                server.shutdown();server.server_close();thread.join(timeout=2)
    def test_same_root_identity_stable_and_different_roots_distinct(self):
        with tempfile.TemporaryDirectory() as t:
            a=Path(t)/'a';b=Path(t)/'b';a.mkdir();b.mkdir()
            one=dashboard.create_server(a,port=0);first=one.root_id;one.server_close()
            two=dashboard.create_server(a,port=0);three=dashboard.create_server(b,port=0)
            try:
                self.assertEqual(first,two.root_id);self.assertNotEqual(first,three.root_id)
            finally:two.server_close();three.server_close()

class PublicExampleTests(unittest.TestCase):
    def test_example_requires_explicit_execution_opt_in(self):
        c=json.loads(Path(__file__).with_name('config.example.json').read_text(encoding='utf-8'))
        self.assertEqual(c['codex_execution_mode'],'disabled')
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',c['codex_args'])
        with self.assertRaises(executor.WorkerError):executor.codex_args_from_config(c)
        self.assertEqual(c['projects'],{})
        self.assertFalse(c['alerts']['enabled'])
        self.assertEqual(c['network_guard']['blocked_country_codes'],[])
        self.assertFalse(c['network_guard']['enabled'])


class PublicServiceLifecycleTests(unittest.TestCase):
    def test_real_console_start_status_stop_preserves_fixture(self):
        import dashboard_identity as identity
        root=Path(__file__).resolve().parents[1]
        fixture=root/'tests/fixtures/synthetic-state'
        before={p.relative_to(fixture).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in fixture.rglob('*') if p.is_file()}
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        args=[sys.executable,str(root/'worker/dashboard_service.py')]
        extra=['--state-root',str(fixture),'--port',str(port),'--read-only']
        def run(action):
            return subprocess.run(args+[action]+extra,capture_output=True,text=True,encoding='utf-8',timeout=20,check=False)
        try:
            start=run('start');self.assertEqual(start.returncode,0,start.stdout+start.stderr)
            status=run('status');self.assertEqual(status.returncode,0,status.stdout+status.stderr)
            self.assertTrue(json.loads(status.stdout)['running'])
            start_again=run('start');self.assertEqual(start_again.returncode,0,start_again.stdout+start_again.stderr)
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3);conn.request('GET','/api/service')
            response=conn.getresponse();obj=json.loads(response.read());conn.close()
            self.assertEqual(obj['root_id'],identity.service_instance_id(fixture,create=False))
            self.assertFalse(obj['allow_controls'])
        finally:
            stop=run('stop')
        self.assertEqual(stop.returncode,0,stop.stdout+stop.stderr)
        self.assertEqual(run('status').returncode,1)
        after={p.relative_to(fixture).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in fixture.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

class LocalIdentityStoreTests(unittest.TestCase):
    def test_registry_stays_outside_source_and_is_random(self):
        import dashboard_identity as identity
        with tempfile.TemporaryDirectory() as t,patch.object(identity.tempfile,'gettempdir',return_value=t):
            root=Path(t)/'state';root.mkdir()
            self.assertIsNone(identity.service_instance_id(root,create=False))
            one=identity.service_instance_id(root)
            self.assertEqual(identity.service_instance_id(root,create=False),one)
            self.assertEqual(list(root.iterdir()),[])
            self.assertRegex(one,r'^[0-9a-f]{64}$')
            self.assertNotEqual(one,hashlib.sha256(str(root.resolve()).encode()).hexdigest())
    def test_corrupt_record_fails_closed(self):
        import dashboard_identity as identity
        with tempfile.TemporaryDirectory() as t,patch.object(identity.tempfile,'gettempdir',return_value=t):
            root=Path(t)/'state';root.mkdir();identity.service_instance_id(root)
            record=next(Path(t).glob('bridge-console-identity-*/*.id'))
            record.write_text('not-an-identity',encoding='ascii')
            with self.assertRaises(identity.state_roots.StateRootError):identity.service_instance_id(root)
    def test_symlink_record_is_rejected(self):
        import dashboard_identity as identity
        with tempfile.TemporaryDirectory() as t,patch.object(identity.tempfile,'gettempdir',return_value=t):
            root=Path(t)/'state';root.mkdir();identity.service_instance_id(root)
            record=next(Path(t).glob('bridge-console-identity-*/*.id'))
            outside=Path(t)/'outside';outside.write_text('a'*64+'\n',encoding='ascii');record.unlink()
            try:record.symlink_to(outside)
            except OSError:self.skipTest('symlink creation is unavailable')
            with self.assertRaises(identity.state_roots.StateRootError):identity.service_instance_id(root)
            self.assertEqual(outside.read_text(encoding='ascii'),'a'*64+'\n')
    def test_hardlink_record_is_rejected(self):
        import dashboard_identity as identity
        with tempfile.TemporaryDirectory() as t,patch.object(identity.tempfile,'gettempdir',return_value=t):
            root=Path(t)/'state';root.mkdir();identity.service_instance_id(root)
            record=next(Path(t).glob('bridge-console-identity-*/*.id'))
            extra=Path(t)/'extra';os.link(record,extra)
            with self.assertRaises(identity.state_roots.StateRootError):identity.service_instance_id(root)
    def test_shared_permissions_are_rejected(self):
        import dashboard_identity as identity
        if os.name=='nt':self.skipTest('POSIX mode bits; Windows user temp ACL requires platform acceptance')
        with tempfile.TemporaryDirectory() as t,patch.object(identity.tempfile,'gettempdir',return_value=t):
            root=Path(t)/'state';root.mkdir();identity.service_instance_id(root)
            directory=next(Path(t).glob('bridge-console-identity-*'));directory.chmod(0o777)
            try:
                with self.assertRaises(identity.state_roots.StateRootError):identity.service_instance_id(root)
            finally:directory.chmod(0o700)

class CurrentPublicScopeTests(unittest.TestCase):
    def test_only_current_operator_source_is_designated(self):
        import operator_safety_gate as gate
        root=Path(__file__).resolve().parents[1]
        self.assertEqual(gate.DESIGNATED_PYTHON,('worker/self_maintenance.py',))
        self.assertEqual(gate.audit(root),())
    def test_missing_current_operator_source_is_not_ignored(self):
        import operator_safety_gate as gate
        with tempfile.TemporaryDirectory() as t:
            self.assertIn('designated-source-missing',{v.rule for v in gate.audit(Path(t))})
    def test_unknown_historical_doc_has_no_merge_exception(self):
        from vnext_runtime.adoption import ReconciliationPolicy,AdoptionValidationError
        policy=ReconciliationPolicy()
        self.assertEqual(policy.documentation_section_union_paths,())
        self.assertEqual(policy.documentation_latest_main_paths,('policies/supervisor.md',))
        with self.assertRaises(AdoptionValidationError):ReconciliationPolicy(documentation_section_union_paths=('docs/unknown-history.md',))
    def test_allowed_reconciliation_paths_exist(self):
        from vnext_runtime.adoption import ReconciliationPolicy
        root=Path(__file__).resolve().parents[1];policy=ReconciliationPolicy()
        for p in (*policy.clean_three_way_paths,*policy.documentation_section_union_paths,*policy.documentation_latest_main_paths,*policy.protocol_kernel_clean_three_way_paths,*policy.boundary_forward_paths):
            with self.subTest(path=p):self.assertTrue((root/p).is_file())

if __name__=='__main__':unittest.main()


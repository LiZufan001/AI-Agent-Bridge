import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bridge_dashboard as dashboard
import bridge_worker as bw
import console_observation as observation
import owner_console_control as control
from codex_lifecycle import run_codex_process
from vnext_runtime.health_aggregator import HealthAggregator

class OwnerConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write('worker/config.local.json', {'projects': {'p': {'enabled': True}}, 'remote_projects_file': ''})
        self.write('supervisor/portfolio.json', {'schema_version':1, 'projects':[{'project_id':'p','owner_selected':True,'owner_paused':False,'priority_rank':10}]})
        self.state = {'protocol_version':2,'project_id':'p','status':'CODEX_RUNNING','generation':2,'latest_command':1,'latest_report':0,'last_reviewed_report':0,
            'active_run':{'run_id':'run-001-abcdef','command_id':1,'claimed_generation':2,'claimed_at':datetime.now(timezone.utc).isoformat(),'lease_expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),'executor':{'model':'gpt-test','reasoning_effort':'high'}}}
        self.write('projects/p/state.json', self.state)
        self.identity = {'run_id':'run-001-abcdef','command_id':1,'generation':2}
        self.directory = self.root/'worker/runtime/p/runs/run-001-abcdef'
        self.directory.mkdir(parents=True)
        self.health()
    def write(self,path,value):
        p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value),encoding='utf-8')
    def health(self,stale=False):
        now=(datetime.now(timezone.utc)-timedelta(seconds=300 if stale else 0)).isoformat()
        h={'worker_pid':os.getpid(),'updated_at':now,'last_poll_at':now,'coordinator_lifecycle':'RUNNING','owner_console_control_version':1,'active_runs':[]}
        self.write('worker/runtime/worker-health.json',h);self.write('worker/runtime/launcher-health.json',h);self.write('worker/runtime/resource-registry.json',[])
    def server(self):
        server=dashboard.create_server(self.root,port=0,allow_controls=True)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return server
    def request(self,server,path='/api/health',method='GET',body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        headers=headers or {}
        if body is not None:body=json.dumps(body)
        connection.request(method,path,body,headers)
        response=connection.getresponse();result=(response.status,response.read(),dict(response.headers));connection.close();return result
    def owner_headers(self,server):
        return {'Origin':f'http://127.0.0.1:{server.server_port}','Content-Type':'application/json','X-Bridge-Owner':'1'}
    def test_stale_running_is_never_green(self):
        self.health(stale=True);snap=HealthAggregator(self.root).collect().to_dict()
        self.assertNotEqual(snap['coordinator']['severity'],'Healthy')
        self.assertEqual(snap['projects'][0]['severity'],'Action Required')
        self.assertFalse(snap['worker']['heartbeat']['fresh'])
    def test_finished_canonical_run_cannot_resurrect_from_worker_summary(self):
        self.state.update(status='REPORT_READY',active_run=None);self.write('projects/p/state.json',self.state)
        self.write('worker/runtime/worker-health.json',{'updated_at':datetime.now(timezone.utc).isoformat(),'active_runs':[{'project_id':'p','run_id':'run-001-abcdef'}]})
        self.assertIsNone(HealthAggregator(self.root).collect().projects[0].active_run)
    def test_future_heartbeat_is_not_healthy(self):
        self.write('worker/runtime/worker-health.json',{'updated_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
        self.assertFalse(HealthAggregator(self.root).collect().worker['heartbeat']['fresh'])
    def test_owner_selected_projects_not_in_local_config_are_visible(self):
        self.write('supervisor/portfolio.json',{'projects':[{'project_id':'other','owner_selected':True,'owner_paused':True}]})
        self.write('projects/other/state.json',dict(self.state,project_id='other',status='REPORT_READY',active_run=None))
        projects=HealthAggregator(self.root).collect().projects
        self.assertEqual({p.project_id for p in projects},{'p','other'})
        self.assertTrue(next(p for p in projects if p.project_id=='other').owner_paused)
    def test_dashboard_never_truncates_project_list_at_32(self):
        self.write('worker/config.local.json',{'projects':{f'p{i}':{'enabled':True} for i in range(45)},'remote_projects_file':''})
        self.assertEqual(len(HealthAggregator(self.root).collect().to_dict()['projects']),46)
    def test_http_boundaries_and_csrf_leave_canonical_state_unchanged(self):
        server=self.server();before=(self.root/'projects/p/state.json').read_bytes()
        for headers in ({'Host':'evil.example'}, {'Sec-Fetch-Site':'cross-site'}, {'Origin':'http://evil.example'}):
            self.assertEqual(self.request(server,headers=headers)[0],403)
        body=dict(self.identity,action='stop',project_id='p')
        self.assertEqual(self.request(server,'/api/control','POST',body)[0],403)
        self.assertEqual(self.request(server,'/api/control','POST',body,{'Origin':f'http://127.0.0.1:{server.server_port}'})[0],415)
        for path in ('/worker/config.local.json','/../projects/p/state.json','/api/run?project=../p&run=../x'):
            self.assertEqual(self.request(server,path)[0],404)
        status,_,headers=self.request(server)
        self.assertEqual(status,200);self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertEqual(before,(self.root/'projects/p/state.json').read_bytes())
    def test_stale_worker_cannot_accept_controls(self):
        self.health(stale=True);server=self.server()
        self.assertEqual(self.request(server,'/api/control','POST',dict(self.identity,action='stop',project_id='p'),self.owner_headers(server))[0],409)
        self.assertFalse((self.directory/'owner-stop.json').exists())
    def test_stop_idempotence_and_stale_identity(self):
        before=(self.root/'projects/p/state.json').read_bytes()
        self.assertEqual(control.request_stop(self.root,'p',self.identity)['status'],'requested')
        self.assertTrue(control.stop_requested(self.root,'p',self.identity))
        control.request_stop(self.root,'p',self.identity)
        with self.assertRaises(control.ControlConflict):control.request_stop(self.root,'p',dict(self.identity,generation=3))
        self.state.update(status='REPORT_READY',active_run=None);self.write('projects/p/state.json',self.state)
        self.assertFalse(control.stop_requested(self.root,'p',self.identity))
        self.assertNotEqual(before,(self.root/'projects/p/state.json').read_bytes())
    def test_pause_uses_existing_cas_and_only_changes_portfolio(self):
        path=self.root/'supervisor/portfolio.json';value=control.read_object(path);before=(self.root/'projects/p/state.json').read_bytes()
        def cas(**kw):
            self.assertTrue(kw['expected'](value));self.assertFalse(kw['expected'](dict(value,schema_version=2)))
            for p,content in kw['payload_builder'](value).items():p.write_text(content,encoding='utf-8')
            return control.read_object(path)
        with patch.object(control.git_store,'publish_cas',side_effect=cas):control.set_project_paused(self.root,'p',True,control.revision(value))
        with self.assertRaises(control.ControlConflict):control.require_project_admission(self.root,'p')
        self.assertEqual(before,(self.root/'projects/p/state.json').read_bytes())
    def test_raw_output_and_credentials_never_reach_api(self):
        secret='PRIVATE_PROMPT_SENTINEL_19'
        (self.directory/'stderr.log').write_text('user\n'+secret+'\nAuthorization: Bearer fake\nexec\n'+secret+'\nthinking\n',encoding='utf-8')
        (self.directory/'stdout.log').write_text(secret,encoding='utf-8')
        observation.record_event(self.directory,'process_exit',{'exit_code':0,'prompt':secret,'token':secret})
        server=self.server();status,raw,_=self.request(server,'/api/run?project=p&run=run-001-abcdef')
        self.assertEqual(status,200);self.assertNotIn(secret.encode(),raw);self.assertNotIn(b'Authorization',raw)
        self.assertIn('正在分析任务'.encode(),raw)
    def test_task_heading_is_screened_and_body_is_never_returned(self):
        path=self.root/'projects/p/commands/command-001.md';path.parent.mkdir(parents=True)
        path.write_text('# Command 001 — Run validation\n\nPRIVATE_PROMPT_SENTINEL\nAuthorization: Bearer value',encoding='utf-8')
        self.assertEqual(observation.command_summary(self.root,'p',1),{'title':'Run validation'})
        path.write_text('# Command 001 — token=SECRET_SENTINEL\n',encoding='utf-8')
        self.assertNotIn('SECRET_SENTINEL',json.dumps(observation.command_summary(self.root,'p',1)))
    def test_stop_uses_process_boundary_and_cannot_publish_success_marker(self):
        control.request_stop(self.root,'p',self.identity)
        helper=self.root/'helper.py';output=self.directory/'final.txt'
        helper.write_text('import time\ntime.sleep(60)\n',encoding='utf-8')
        result=run_codex_process(args=[sys.executable,str(helper)],workdir=self.root,output_file=output,
            stdout_log_path=self.directory/'out.log',stderr_log_path=self.directory/'err.log',prompt='fixture',
            execution_timeout_seconds=5,final_grace_timeout_seconds=.1,cleanup_timeout_seconds=2,marker_stable_seconds=.1,
            poll_interval_seconds=.02,max_log_bytes=1024,max_final_message_bytes=1024,marker_parser=bw.parse_final_result,
            stop_check=lambda:control.stop_requested(self.root,'p',self.identity))
        self.assertEqual(result.termination_reason,'owner_stop');self.assertTrue(result.forced_cleanup)
        self.assertIsNone(result.cleanup_error);self.assertEqual(bw.execution_outcome(result),'FAILED')
        self.assertEqual(result.marker_result,'PROCESS_TERMINATED_WITHOUT_MARKER')
        self.assertIn('OWNER_STOP',bw.intended_execution_error(outcome='FAILED',run_result=result,exit_code=result.exit_code))
        from dataclasses import replace
        uncertain=replace(result,cleanup_error='fixture cleanup uncertain')
        self.assertEqual(bw.intended_report_status(previous_status='COMMAND_READY',kind='EXECUTE',outcome='FAILED',final_message='',run_result=uncertain),'RECOVERY_REQUIRED')
    def test_runtime_refresh_respects_isolation_and_preserves_guard(self):
        import self_maintenance as sm
        before=sm._protected_snapshot(self.root)
        isolated=Path(self.temp.name)/'isolated'
        bw.refresh_active_run_health(self.root,runtime_root=isolated)
        self.assertEqual(before,sm._protected_snapshot(self.root))
        self.assertTrue((isolated/'worker-health.json').exists())

if __name__=='__main__':unittest.main()

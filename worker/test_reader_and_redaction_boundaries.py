"""Synthetic reader and report regression; no credentials, remote operations or real State."""
import json, hashlib, os, shutil, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import state_roots as roots
import supervisor_publication_gateway_core as gate
import report_builder as reports
from test_report_builder import _worker_kwargs, _lifecycle_result
from test_state_fixture import make_state

VALUE='SYNTHETIC_CREDENTIAL_SENTINEL_05'
def request():
 meta={'command_id':6,'source':'scheduled_chatgpt','based_on_report':5,'expected_generation':11,'kind':'EXECUTE'}
 command='<!-- bridge-command: '+json.dumps(meta)+' -->\n# Synthetic command\nNo execution requested.\n'
 body={'schema_version':1,'request_id':'synthetic-05','project_id':'p','command_id':6,'source':'scheduled_chatgpt','kind':'EXECUTE','based_on_report':5,'expected_generation':11,'command_sha256':hashlib.sha256(command.encode()).hexdigest(),'command_content':command,'created_at':'2026-01-01T00:00:00Z'}
 return gate.StagedPublicationRequest.from_bytes(json.dumps(body).encode(),filename_request_id='synthetic-05')

class ReaderBoundaryTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(prefix='cp05-synthetic-');self.addCleanup(self.t.cleanup)
  self.base=Path(self.t.name);self.root=make_state(self.base/'state',git=False)
  self.outside=self.base/'outside';self.outside.mkdir()
 def link(self,target,link):link.symlink_to(target,target_is_directory=target.is_dir())
 def test_projects_layout_link_rejected(self):
  (self.root/'projects').rmdir();self.link(self.outside,self.root/'projects')
  with self.assertRaises(roots.StateRootError):roots.resolve_state_root(self.root,environ={})
 def test_supervisor_layout_link_rejected(self):
  (self.root/'supervisor').rmdir();self.link(self.outside,self.root/'supervisor')
  with self.assertRaises(roots.StateRootError):roots.resolve_state_root(self.root,environ={})
 def test_worker_layout_link_rejected(self):
  self.link(self.outside,self.root/'worker')
  with self.assertRaises(roots.StateRootError):roots.resolve_state_root(self.root,environ={})
 def test_configuration_parent_link_refused(self):
  p=self.outside/'config.json';p.write_text('{"x":1}');self.link(self.outside,self.root/'linked')
  with self.assertRaises(roots.StateRootError):roots._object(self.root/'linked/config.json')
 def test_configuration_hardlink_refused(self):
  p=self.outside/'config.json';p.write_text('{"x":1}');os.link(p,self.root/'config.json')
  with self.assertRaises(roots.StateRootError):roots._object(self.root/'config.json')
 def test_configuration_regular_file_still_works(self):
  p=self.root/'config.json';p.write_text('{"x":1}')
  self.assertEqual(roots._object(p),{'x':1})
 def test_configuration_oversize_refused(self):
  p=self.root/'config.json';p.write_text('{"x": "12345678"}')
  with self.assertRaises(roots.StateRootError):roots._object(p,max_bytes=5)
 def test_gateway_root_link_refused_at_construction(self):
  self.link(self.root,self.base/'linked-root')
  with self.assertRaises(ValueError):gate.SupervisorPublicationGateway(self.base/'linked-root')
 def test_gateway_request_directory_link_refused(self):
  p=self.root/gate.STAGED_PUBLICATION_ROOT;p.mkdir(parents=True);self.link(self.outside,p/'requests')
  with self.assertRaises(ValueError):gate.SupervisorPublicationGateway(self.root)
 def test_gateway_rejects_projects_link_before_read(self):
  (self.root/'projects').rmdir();self.link(self.outside,self.root/'projects')
  p=self.outside/'p';p.mkdir();(p/'commands').mkdir();(p/'reports').mkdir()
  (p/'state.json').write_text(json.dumps({'protocol_version':2,'project_id':'p','status':'REPORT_READY','generation':999,'latest_command':5,'latest_report':5,'active_run':None}))
  g=gate.SupervisorPublicationGateway(self.root)
  with patch.object(gate,'_read_state',wraps=gate._read_state) as read:
   r=g._consume_one(self.root/'unused.json',request());self.assertEqual(r.reason,'project_path_invalid');read.assert_not_called()
 def test_state_reader_parent_link_refused(self):
  p=self.outside/'state.json';p.write_text('{"x":1}');self.link(self.outside,self.root/'linked')
  with self.assertRaises(gate.StagedPublicationError):gate._read_state(self.root/'linked/state.json')
 def test_state_reader_hardlink_refused(self):
  p=self.outside/'state.json';p.write_text('{"x":1}');os.link(p,self.root/'state.json')
  with self.assertRaises(gate.StagedPublicationError):gate._read_state(self.root/'state.json')
 def test_state_reader_duplicate_keys_refused(self):
  p=self.root/'state.json';p.write_text('{"generation":1,"generation":2}')
  with self.assertRaises(gate.StagedPublicationError):gate._read_state(p)
 def test_state_reader_nonfinite_refused(self):
  p=self.root/'state.json';p.write_text('{"value":NaN}')
  with self.assertRaises(gate.StagedPublicationError):gate._read_state(p)
 def test_state_reader_regular_file_still_works(self):
  p=self.root/'state.json';p.write_text('{"generation":1}')
  self.assertEqual(gate._read_state(p),{'generation':1})
 def test_root_dotdot_is_not_normalized_away(self):
  path=self.root/'supervisor'/'..'
  with self.assertRaises(roots.StateRootError):roots.resolve_state_root(path,environ={})

class ReportBoundaryTests(unittest.TestCase):
 def test_quoted_json_keys(self):
  for k in ['password','token','api_key','client_secret','Authorization','Cookie','access_token','refresh_token']:
   with self.subTest(key=k):self.assertNotIn(VALUE,reports.redact_diagnostics(json.dumps({k:VALUE})))
 def test_json_encoded_key(self):
  self.assertNotIn(VALUE,reports.redact_diagnostics('{"\\u0070assword": "'+VALUE+'"}'))
 def test_quoted_spaces_and_escaped_quote(self):
  for val in [VALUE+' with spaces',VALUE+'"quoted"tail',VALUE+'\nmultiline']:
   with self.subTest(value_type=len(val)):self.assertNotIn(VALUE,reports.redact_diagnostics(json.dumps({'password':val})))
 def test_single_quoted_dict_and_assignment(self):
  for s in ["{'password': '"+VALUE+" with spaces'}",'password="'+VALUE+' with spaces"']:
   self.assertNotIn(VALUE,reports.redact_diagnostics(s))
 def test_structured_secret_value(self):
  for v in [[VALUE],{'nested':[VALUE,{'again':VALUE}]}]:self.assertNotIn(VALUE,reports.redact_diagnostics(json.dumps({'secret':v})))
 def test_unterminated_quoted_secret(self):
  self.assertNotIn(VALUE,reports.redact_diagnostics('password="text\n'+VALUE))
 def test_manual_report_uses_redaction(self):
  s=reports.build_manual_report(project_id='p',command_id=1,outcome='SUCCESS',final_message='Authorization: Bearer '+VALUE,active_run={},completed_at='2026-01-01T00:00:00Z',executor_host='synthetic-host')
  self.assertNotIn(VALUE,s);self.assertIn('[REDACTED]',s)
 def test_worker_metadata_does_not_bypass_redaction(self):
  s=reports.build_worker_report(**_worker_kwargs(run_result=_lifecycle_result(cleanup_error=json.dumps({'password':VALUE}))))
  self.assertNotIn(VALUE,s)
 def test_manual_metadata_does_not_bypass_redaction(self):
  s=reports.build_manual_report(project_id='p',command_id=1,outcome='SUCCESS',final_message='Fine.',active_run={},completed_at='2026-01-01T00:00:00Z',executor_host='synthetic-host\npassword='+VALUE)
  self.assertNotIn(VALUE,s)
 def test_long_stderr_redacted_before_tail(self):
  s=reports.build_worker_report(**_worker_kwargs(outcome='FAILED',exit_code=1,stderr='password='+VALUE*400))
  self.assertNotIn(VALUE,s)
 def test_basic_authorization_not_just_scheme(self):
  self.assertNotIn(VALUE,reports.redact_diagnostics('Authorization: Basic '+VALUE))
 def test_set_cookie_header_value_removed(self):
  self.assertNotIn(VALUE,reports.redact_diagnostics('Set-Cookie: session='+VALUE+'; Secure'))
 def test_ordinary_prose_unchanged(self):
  s='Result: PASS\nThe password field is documented.\n- generation: 3\n'
  self.assertEqual(reports.redact_diagnostics(s),s)
 def test_idempotent(self):
  s='Authorization: Bearer '+VALUE+'\n'+json.dumps({'password':VALUE})+'\ntoken='+VALUE
  one=reports.redact_diagnostics(s);self.assertEqual(reports.redact_diagnostics(one),one)
 def test_labelled_encoding_removed_without_decoding(self):
  import base64
  v=base64.b64encode(VALUE.encode()).decode();self.assertNotIn(v,reports.redact_diagnostics(json.dumps({'password':v})))

class AdditionalReportTests(unittest.TestCase):
 def test_cli_secret_flags(self):
  for s in ['--password='+VALUE,'--api-key='+VALUE,'--token='+VALUE]:
   with self.subTest(flag=s.split('=')[0]):self.assertNotIn(VALUE,reports.redact_diagnostics(s))
 def test_multiline_labelled_value(self):
  self.assertNotIn(VALUE,reports.redact_diagnostics('password:\n  '+VALUE))
 def test_manual_real_wrapper_and_pending_file(self):
  import bridge_manual,bridge_common
  text=bridge_manual.manual_report_markdown(project_id='p',command_id=1,outcome='SUCCESS',final_message=json.dumps({'password':VALUE}),active_run={})
  with tempfile.TemporaryDirectory(prefix='cp05-pending-synthetic-') as tmp:
   bridge_common.save_pending_report(Path(tmp),1,text,'synthetic temporary publication failure')
   stored=(Path(tmp)/'pending-report-001.md').read_text(encoding='utf-8')
   self.assertEqual(stored,text);self.assertNotIn(VALUE,stored)
 def test_redaction_does_not_claim_identity_anonymisation(self):
  text=reports.build_manual_report(project_id='synthetic-p',command_id=1,outcome='SUCCESS',final_message='Done.',active_run={},completed_at='2026-01-01T00:00:00Z',executor_host='synthetic-host')
  self.assertIn('synthetic-host',text);self.assertIn('2026-01-01T00:00:00Z',text)

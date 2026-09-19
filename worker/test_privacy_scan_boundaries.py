"""Synthetic scanner-output regression. No real identities or network access."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import state_roots
sys.path.insert(0, str(state_roots.engine_root() / 'tools'))
import privacy_scan as scan

IDENTITY = 'FictionalOrchidOperator719'

class ScanBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cp06-fixture-')
        self.base = Path(self.tmp.name)
        self.root = self.base / 'candidate'; self.root.mkdir()
        self.f = self.root / (IDENTITY + '.md')
        self.f.write_text('generic documentation\n', encoding='utf-8')
    def tearDown(self): self.tmp.cleanup()
    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', scan.__file__, '--root', str(self.root), '--tree', *map(str,args)], capture_output=True, text=True, timeout=12)
    def assert_safe(self, text):
        for value in (IDENTITY, IDENTITY.casefold(), hashlib.sha256(IDENTITY.casefold().encode()).hexdigest(), str(self.base), self.f.name):
            self.assertNotIn(value, text)
    def test_inspect_does_not_derive_match_fingerprint(self):
        found = scan.inspect('generic.md', IDENTITY.encode(), mode='public', denied={scan.sha(IDENTITY.casefold().encode())})
        self.assertTrue(found)
        self.assertNotIn('match_sha256', json.dumps(found))
        self.assertNotIn(scan.sha(IDENTITY.casefold().encode()), json.dumps(found))
    def test_cli_all_three_outputs_aggregate(self):
        self.f.write_text('person@mail.invalid\n', encoding='utf-8')
        js, md = self.base/'result.json', self.base/'result.md'
        result = self.cli('--json',js,'--report',md)
        self.assertEqual(result.returncode,1); self.assertEqual(result.stderr,'')
        for text in (result.stdout,js.read_text(),md.read_text()):
            self.assert_safe(text)
            self.assertNotIn('person@mail.invalid',text)
            self.assertNotIn(hashlib.sha256(self.f.read_bytes()).hexdigest(),text)
        report = json.loads(result.stdout)
        self.assertEqual(report['publication_authorized'],False)
        self.assertEqual(report['finding_counts']['personal-email'],1)
        self.assertNotIn('unexplained_findings',report)
    def test_clean_scan_never_authorizes_release(self):
        result=self.cli(); self.assertEqual(result.returncode,0)
        report=json.loads(result.stdout)
        self.assertIs(report['publication_authorized'],False)
        self.assertEqual(report['scope'],'content_lint_only')
    def test_policy_error_no_path_or_traceback(self):
        result=self.cli('--identifier-policy',self.base/(IDENTITY+'.json'))
        self.assertEqual(result.returncode,1); self.assertEqual(result.stderr,''); self.assert_safe(result.stdout)
        self.assertEqual(json.loads(result.stdout)['reason_code'],'scan_input_or_environment_error')
    def test_bad_policy_schema_is_fixed_error(self):
        p=self.base/'policy.json';p.write_text('[]')
        result=self.cli('--identifier-policy',p)
        self.assertEqual(result.returncode,1); self.assertEqual(result.stderr,''); self.assert_safe(result.stdout)
    def test_malformed_allowlist_no_traceback(self):
        p=self.base/'allow.json';p.write_text(json.dumps([{'path':IDENTITY}]))
        result=self.cli('--allowlist',p)
        self.assertEqual(result.returncode,1);self.assertEqual(result.stderr,'');self.assert_safe(result.stdout)
    def test_source_cannot_be_output(self):
        before=self.f.read_bytes();result=self.cli('--json',self.f)
        self.assertEqual(result.returncode,1);self.assertEqual(self.f.read_bytes(),before)
        self.assert_safe(result.stdout)
    def test_new_output_inside_root_refused(self):
        target=self.root/'generated.json';result=self.cli('--json',target)
        self.assertEqual(result.returncode,1);self.assertFalse(target.exists())
    def test_existing_output_unchanged(self):
        out=self.base/'result.json';out.write_text('preserve')
        result=self.cli('--json',out)
        self.assertEqual(result.returncode,1);self.assertEqual(out.read_text(),'preserve')
    def test_output_symlink_unchanged(self):
        out=self.base/'alias';out.symlink_to(self.f)
        before=self.f.read_bytes();result=self.cli('--report',out)
        self.assertEqual(result.returncode,1);self.assertEqual(self.f.read_bytes(),before)
    def test_same_output_refused_before_writing(self):
        out=self.base/'same';result=self.cli('--json',out,'--report',out)
        self.assertEqual(result.returncode,1);self.assertFalse(out.exists())
    def test_second_existing_output_does_not_create_first(self):
        a,b=self.base/'new',self.base/'old';b.write_text('preserve')
        result=self.cli('--json',a,'--report',b)
        self.assertEqual(result.returncode,1);self.assertFalse(a.exists());self.assertEqual(b.read_text(),'preserve')
    def test_legacy_match_digest_allowlist_is_rejected(self):
        (self.root/'credentials.json').write_text('{}')
        item=scan.audit(self.root,tree=True)['unexplained_findings'][0]
        p=self.base/'allow.json';p.write_text(json.dumps([{**item,'match_sha256':'0'*64,'reason':'synthetic'}]))
        with self.assertRaises(ValueError):scan.audit(self.root,tree=True,allowlist=p)
    def test_stale_private_allowlist_details_never_echoed(self):
        p=self.base/'allow.json'
        p.write_text(json.dumps([{'path':IDENTITY+'.md','line':1,'rule':'personal-email','file_sha256':'0'*64,'reason':IDENTITY}]))
        result=self.cli('--allowlist',p)
        self.assertEqual(result.returncode,1);self.assertEqual(result.stderr,'');self.assert_safe(result.stdout)
    def test_nonexistent_root_does_not_pass(self):
        self.f.unlink();self.root.rmdir();result=self.cli()
        self.assertEqual(result.returncode,1);self.assertEqual(result.stderr,'')
    def test_empty_tree_does_not_pass(self):
        self.f.unlink();result=self.cli()
        self.assertEqual(result.returncode,1);self.assertEqual(result.stderr,'')
    def test_broken_symlink_does_not_disappear(self):
        (self.root/'alias').symlink_to(self.base/'missing');result=self.cli()
        self.assertEqual(result.returncode,1);self.assertEqual(result.stderr,'')
    def test_root_symlink_not_silently_resolved(self):
        alias=self.base/'root_alias';alias.symlink_to(self.root,target_is_directory=True)
        result=subprocess.run([sys.executable,'-B',scan.__file__,'--root',str(alias),'--tree'],capture_output=True,text=True,timeout=12)
        self.assertEqual(result.returncode,1);self.assertEqual(result.stderr,'')
    def test_history_request_outside_git_safe_error(self):
        result=self.cli('--history')
        self.assertEqual(result.returncode,1);self.assertEqual(result.stderr,'');self.assert_safe(result.stdout)
    def test_unknown_cli_argument_no_input_echo(self):
        result=self.cli('--'+IDENTITY)
        self.assertEqual(result.returncode,2);self.assert_safe(result.stdout+result.stderr)

    def test_dotdot_root_still_protects_candidate(self):
        middle=self.base/'unused';middle.mkdir()
        before=self.f.read_bytes()
        result=subprocess.run([sys.executable,'-B',scan.__file__,'--root',str(middle/'..'/'candidate'),'--tree','--json',str(self.f)],capture_output=True,text=True,timeout=12)
        self.assertEqual(result.returncode,1);self.assertEqual(self.f.read_bytes(),before)
    def test_oversized_file_not_read_into_memory(self):
        from unittest.mock import patch
        with patch.object(scan,'MAX_FILE_BYTES',4), patch.object(Path,'read_bytes',side_effect=AssertionError('must not read')):
            report=scan.audit(self.root,tree=True)
        self.assertFalse(report['pass'])
        self.assertEqual(report['unexplained_findings'][0]['rule'],'oversized-unscanned-file')

if __name__=='__main__':unittest.main(verbosity=2)

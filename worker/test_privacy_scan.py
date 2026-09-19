import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import state_roots
sys.path.insert(0,str(state_roots.engine_root()/'tools'))
import privacy_scan as scan

class PrivacyScanTests(unittest.TestCase):
    def test_injected_paths_accounts_credentials_and_real_state_fail(self):
        samples=[('projects/real/state.json',b'{}','concrete-state-or-runtime-path'),
            ('a.txt',('ghp_'+'a'*30).encode(),'credential-value'),
            ('a.txt',('ou_'+'z'*24).encode(),'account-identifier'),
            ('a.txt',(chr(68)+':\\Users\\personal\\file.txt').encode(),'absolute-windows-path'),
            ('credentials.json',b'{}','secret-or-local-config-file'),
            ('a.md',('DESKTOP-'+'PRIVATEHOST').encode(),'machine-hostname'),
            ('a.md',('person@'+'mail.invalid').encode(),'personal-email')]
        for path,raw,rule in samples:
            with self.subTest(rule=rule):self.assertIn(rule,{x['rule'] for x in scan.inspect(path,raw,mode='public',denied=set())})
    def test_compound_known_identifier_detected_without_printing_value(self):
        token='private-lab';denied={scan.sha(token.encode())}
        findings=scan.inspect('a.md',(token+' project').encode(),mode='public',denied=denied)
        self.assertTrue(findings);self.assertNotIn(token,json.dumps(findings))

    def test_external_identifier_policy_detects_separator_and_case_variants_without_echoing_value(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'tree'; root.mkdir()
            (root / 'notes.md').write_text(
                'example_collector Example.Collector EXAMPLE-COLLECTOR FooBar\n',
                encoding='utf-8',
            )
            policy = Path(d) / 'identifier-policy.json'
            policy.write_text(
                json.dumps({'private_identifier_sha256': [scan.sha(b'examplecollector')]}),
                encoding='utf-8',
            )
            result = scan.audit(root, tree=True, identifier_policy=policy)
            self.assertFalse(result['pass'])
            self.assertEqual(
                {item['rule'] for item in result['unexplained_findings']},
                {'known-private-identifier'},
            )
            rendered = json.dumps(result)
            for variant in ('examplecollector', 'example_collector', 'Example.Collector', 'EXAMPLE-COLLECTOR'):
                self.assertNotIn(variant, rendered)

    def test_default_public_scan_has_no_private_identifier_policy(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'tree'; root.mkdir()
            (root / 'notes.md').write_text('example_collector\n', encoding='utf-8')
            result = scan.audit(root, tree=True)
            self.assertTrue(result['pass'])
            self.assertNotIn('known-private-identifier', {item['rule'] for item in result['unexplained_findings']})
    def test_allowlist_is_exact_and_stale_allowlist_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'tree';root.mkdir();(root/'credentials.json').write_text('{}')
            raw=scan.audit(root,tree=True);item=raw['unexplained_findings'][0]
            allow=Path(d)/'allow.json';allow.write_text(json.dumps([{**item,'reason':'synthetic injection only'}]))
            self.assertTrue(scan.audit(root,tree=True,allowlist=allow)['pass'])
            (root/'credentials.json').write_text('{"changed":true}')
            self.assertFalse(scan.audit(root,tree=True,allowlist=allow)['pass'])
    def test_clean_history_then_removed_credential_is_detected_in_history(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            def git(*args):subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)
            git('init','-q');git('config','user.name','Synthetic Audit');git('config','user.email','audit@example.test')
            (root/'README.md').write_text('synthetic\n');git('add','.');git('commit','-qm','root')
            self.assertTrue(scan.audit(root,history=True,require_clean_history=True)['pass'])
            (root/'accidental.txt').write_text('ghp_'+'x'*30);git('add','.');git('commit','-qm','accidental fixture')
            git('rm','accidental.txt');git('commit','-qm','remove fixture')
            self.assertTrue(scan.audit(root)['pass'])
            result=scan.audit(root,history=True,require_clean_history=True)
            self.assertFalse(result['pass']);self.assertIn('credential-value',{x['rule'] for x in result['unexplained_findings']})

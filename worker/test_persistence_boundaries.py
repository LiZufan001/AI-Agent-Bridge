"""Synthetic persistence boundary probes; all writes stay inside disposable temporary roots."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import git_store as s

class PersistenceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cp04-synthetic-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'; self.repo.mkdir()
        self.outside = self.base / 'outside'; self.outside.mkdir()
        self.sentinel = self.outside / 'sentinel.txt'; self.sentinel.write_bytes(b'UNCHANGED')
    def attempt(self, payloads):
        # Only validation rejection counts. A later Git failure is not proof.
        with patch.object(s, '_write_payload_atomic', side_effect=AssertionError('write reached')) as writer, \
             patch.object(s, 'git', side_effect=AssertionError('Git reached')) as git, \
             patch.object(s, 'git_mutation_gate', side_effect=AssertionError('mutation gate reached')) as gate:
            with self.assertRaises((s.WorkerError, OSError, ValueError, TypeError)):
                s.commit_payloads(self.repo, payloads, 'synthetic boundary probe')
            writer.assert_not_called(); git.assert_not_called(); gate.assert_not_called()
    def test_outside_write_is_refused_before_mutation(self):
        self.attempt({self.sentinel: b'REPLACED'})
        self.assertEqual(self.sentinel.read_bytes(), b'UNCHANGED')
    def test_entire_batch_validated_before_first_write(self):
        p = self.repo / 'first.txt'
        self.attempt({p: 'first', self.sentinel: b'REPLACED'})
        self.assertFalse(p.exists()); self.assertEqual(self.sentinel.read_bytes(), b'UNCHANGED')
    def test_entire_batch_types_validated_before_first_write(self):
        p = self.repo / 'first.txt'
        self.attempt({p: 'first', self.repo/'bad.txt': 123})
        self.assertFalse(p.exists())
    def test_parent_link_refused(self):
        (self.repo/'linked').symlink_to(self.outside, target_is_directory=True)
        self.attempt({self.repo/'linked'/'sentinel.txt': b'REPLACED'})
        self.assertEqual(self.sentinel.read_bytes(), b'UNCHANGED')
    def test_leaf_link_refused_not_replaced(self):
        p=self.repo/'leaf.txt'; p.symlink_to(self.sentinel)
        self.attempt({p: b'REPLACED'})
        self.assertTrue(p.is_symlink()); self.assertEqual(self.sentinel.read_bytes(), b'UNCHANGED')
    def test_fixed_temp_link_cannot_clobber_other_file(self):
        p=self.repo/'safe.txt'; old_temp=self.repo/'safe.txt.tmp'; old_temp.symlink_to(self.sentinel)
        s._write_payload_atomic(p,b'new exact\r\nbytes')
        self.assertEqual(p.read_bytes(), b'new exact\r\nbytes')
        self.assertEqual(self.sentinel.read_bytes(), b'UNCHANGED')
        self.assertTrue(old_temp.is_symlink())
    def test_reserved_git_metadata_refused(self):
        p=self.repo/'.git'/'config'
        self.attempt({p: 'not a git config'})
        self.assertFalse(p.exists())
    def test_dotdot_refused(self):
        self.attempt({self.repo/'..'/'outside'/'sentinel.txt': b'REPLACED'})
        self.assertEqual(self.sentinel.read_bytes(), b'UNCHANGED')
    def test_payload_ancestry_conflict_refused_without_writes(self):
        p=self.repo/'a'
        self.attempt({p:'parent',p/'child':'child'})
        self.assertFalse(p.exists())
    def test_portable_case_collision_refused(self):
        a, b = self.repo/'a.txt', self.repo/'A.txt'
        payloads = {a:'one', b:'two'}
        if os.name == 'nt':
            self.assertEqual(a, b); self.assertEqual(hash(a), hash(b))
            self.assertEqual(len(payloads), 1); self.assertEqual(list(payloads.values()), ['two'])
            self.skipTest('Windows Path dict collapses case aliases before API; Linux dual-Path assertion retained')
        self.assertEqual(len(payloads), 2)
        self.attempt(payloads)
        self.assertFalse((self.repo/'a.txt').exists()); self.assertFalse((self.repo/'A.txt').exists())
    def test_case_input_cardinality(self):
        a, b = self.repo/'a.txt', self.repo/'A.txt'
        payloads = {a:'one', b:'two'}
        self.assertEqual(len(payloads), 1 if os.name == 'nt' else 2)
        self.assertEqual(a == b, os.name == 'nt')
        if os.name == 'nt': self.assertEqual(list(payloads.values()), ['two'])
    def test_portable_windows_alias_refused(self):
        for name in ['nul.txt','aux','x:stream','trailing.','trailing ','CON','lpt1.log','parent./child.txt','parent /child.txt']:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory(prefix='cp07-r1-alias-') as tmp:
                    previous = self.repo
                    self.repo = Path(tmp)
                    try:
                        self.attempt({self.repo/'first.txt': 'first', self.repo/name: 'bad'})
                        self.assertEqual(list(self.repo.iterdir()), [])
                    finally: self.repo = previous
    def test_trailing_alias_preserves_legal_sentinel(self):
        for name in ['trailing.', 'trailing ', 'parent./child.txt', 'parent /child.txt']:
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix='cp07-r1-sentinel-') as tmp:
                previous = self.repo; self.repo = Path(tmp)
                try:
                    legal=self.repo.joinpath(*(part.rstrip('. ') for part in Path(name).parts))
                    legal.parent.mkdir(parents=True, exist_ok=True); legal.write_bytes(b'LEGAL_SENTINEL')
                    self.attempt({self.repo/'first.txt':'first', self.repo/name:'bad'})
                    self.assertEqual(legal.read_bytes(), b'LEGAL_SENTINEL')
                    self.assertFalse((self.repo/'first.txt').exists())
                    self.assertEqual(sorted(p.relative_to(self.repo).as_posix() for p in self.repo.rglob('*') if p.is_file()), [legal.relative_to(self.repo).as_posix()])
                finally: self.repo = previous
    def test_lock_link_refused_without_target_mutation(self):
        p=self.repo/'worker/runtime/git-store.lock';p.parent.mkdir(parents=True);p.symlink_to(self.sentinel)
        try:
            with s.git_mutation_gate(self.repo): pass
        except (s.WorkerError,OSError,ValueError): pass
        else:self.fail('linked lock accepted')
        self.assertEqual(self.sentinel.read_bytes(), b'UNCHANGED')
    def test_lock_hardlink_refused_without_target_mutation(self):
        p=self.repo/'worker/runtime/git-store.lock';p.parent.mkdir(parents=True);os.link(self.sentinel,p)
        try:
            with s.git_mutation_gate(self.repo): pass
        except (s.WorkerError,OSError,ValueError): pass
        else:self.fail('hardlinked lock accepted')
        self.assertEqual(self.sentinel.read_bytes(), b'UNCHANGED')
    def test_repeated_lock_does_not_append(self):
        for _ in range(5):
            with s.git_mutation_gate(self.repo):
                with s.git_mutation_gate(self.repo): pass
        self.assertEqual((self.repo/'worker/runtime/git-store.lock').read_bytes(),b'0')
    @unittest.skipIf(os.name=='nt','POSIX independent-process contention probe; Windows real acceptance separate')
    def test_waiting_process_does_not_write_lock_before_acquisition(self):
        import fcntl, selectors
        p=self.repo/'worker/runtime/git-store.lock';p.parent.mkdir(parents=True);p.write_bytes(b'0')
        handle=p.open('r+b');fcntl.flock(handle,fcntl.LOCK_EX)
        script='''import fcntl,sys,git_store\nfrom pathlib import Path\noriginal=fcntl.flock\ndef observed(fd,op):\n if op==fcntl.LOCK_EX: print("ATTEMPT",flush=True)\n return original(fd,op)\nfcntl.flock=observed\nwith git_store.git_mutation_gate(Path(sys.argv[1])): print("ACQUIRED",flush=True)\n'''
        child_env=dict(os.environ, PYTHONPATH=str(Path(s.__file__).resolve().parent))
        child=subprocess.Popen([sys.executable,'-B','-c',script,str(self.repo)],env=child_env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            sel=selectors.DefaultSelector();sel.register(child.stdout,selectors.EVENT_READ)
            self.assertTrue(sel.select(8),'child did not reach lock');self.assertEqual(child.stdout.readline().strip(),'ATTEMPT');sel.close()
            self.assertEqual(p.read_bytes(),b'0')
        finally:
            fcntl.flock(handle,fcntl.LOCK_UN);handle.close()
            try:out,err=child.communicate(timeout=8)
            except subprocess.TimeoutExpired:child.kill();child.communicate();raise
        self.assertEqual(child.returncode,0,err);self.assertIn('ACQUIRED',out)
    def test_exact_bytes_real_git_commit(self):
        def git(*args):
            return subprocess.run(['git',*args],cwd=self.repo,capture_output=True,check=True)
        git('init');git('config','user.name','Synthetic Reviewer');git('config','user.email','review@example.invalid');git('config','core.autocrlf','true')
        data=b'first\r\nsecond\r\n';p=self.repo/'records'/'command.md'
        self.assertTrue(s.commit_payloads(self.repo,{p:data},'synthetic initial'))
        self.assertEqual(git('show','HEAD:records/command.md').stdout,data)
        self.assertEqual(p.read_bytes(),data)
        self.assertFalse(s.commit_payloads(self.repo,{p:data},'synthetic unchanged'))
    def test_atomic_writer_cleans_up_after_replace_error(self):
        p=self.repo/'target.txt';p.write_bytes(b'OLD')
        with patch.object(Path,'replace',side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):s._write_payload_atomic(p,b'NEW')
        self.assertEqual(p.read_bytes(),b'OLD')
        self.assertEqual(sorted(x.name for x in self.repo.iterdir()),['target.txt'])

if __name__=='__main__':unittest.main(verbosity=2)

"""Post-fix helper contract, bounded resource reads and controlled final-link substitution."""
import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import state_roots as roots
class ReaderContractTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)
 def test_missing_file_safe_error(self):
  with self.assertRaises(roots.StateRootError) as c:roots.read_regular_bytes(self.root/'not-here',max_bytes=5)
  self.assertNotIn(str(self.root),str(c.exception))
 @unittest.skipUnless(callable(getattr(os, 'mkfifo', None)), 'Unix path FIFO requires os.mkfifo; Windows regular-file and link contracts run separately')
 def test_fifo_refused_before_open(self):
  p=self.root/'fifo';os.mkfifo(p)
  with patch.object(roots.os,'open',side_effect=AssertionError('open reached')):
   with self.assertRaises(roots.StateRootError):roots.read_regular_bytes(p,max_bytes=100)
 def test_size_limit_and_empty_file(self):
  p=self.root/'data';p.write_bytes(b'')
  self.assertEqual(roots.read_regular_bytes(p,max_bytes=0),b'')
  p.write_bytes(b'12345');self.assertEqual(roots.read_regular_bytes(p,max_bytes=5),b'12345')
  with self.assertRaises(roots.StateRootError):roots.read_regular_bytes(p,max_bytes=4)
 def test_directory_refused_before_open(self):
  p=self.root/'directory';p.mkdir()
  with patch.object(roots.os,'open',side_effect=AssertionError('open reached')):
   with self.assertRaises(roots.StateRootError):roots.read_regular_bytes(p,max_bytes=100)
 def test_invalid_limits(self):
  p=self.root/'data';p.write_bytes(b'a')
  for size in [True,-1,1.5,None]:
   with self.subTest(kind=type(size).__name__):
    with self.assertRaises(roots.StateRootError):roots.read_regular_bytes(p,max_bytes=size)
 def test_final_symlink_substitution_denied(self):
  p=self.root/'data';p.write_bytes(b'{}');other=self.root/'other';other.write_bytes(b'{"synthetic":1}')
  original=os.open
  def switched(path,*a,**k):
   p.unlink();p.symlink_to(other);return original(path,*a,**k)
  with patch.object(roots.os,'open',side_effect=switched):
   with self.assertRaises(roots.StateRootError):roots.read_regular_bytes(p,max_bytes=100)
 def test_changed_file_identity_denied(self):
  p=self.root/'data';p.write_bytes(b'{}');other=self.root/'other';other.write_bytes(b'{"synthetic":1}')
  original=os.open
  def switched(path,*a,**k):other.replace(p);return original(path,*a,**k)
  with patch.object(roots.os,'open',side_effect=switched):
   with self.assertRaises(roots.StateRootError):roots.read_regular_bytes(p,max_bytes=100)

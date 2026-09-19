"""Run Worker regression and fail if any self-maintenance protected path changes."""
from __future__ import annotations
import argparse
import hashlib
import faulthandler
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'worker'))
import self_maintenance


class DiagnosticResult(unittest.TextTestResult):
    def startTest(self, test):
        faulthandler.dump_traceback_later(90, repeat=False, exit=True)
        super().startTest(test)

    def stopTest(self, test):
        faulthandler.cancel_dump_traceback_later()
        super().stopTest(test)


def source_snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and '.git' not in p.relative_to(root).parts
            and '__pycache__' not in p.relative_to(root).parts and p.suffix != '.pyc'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', default='test*.py')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source_before = source_snapshot(ROOT)
    before = dict(self_maintenance._protected_snapshot(ROOT).entries)
    result = unittest.TextTestRunner(verbosity=2, resultclass=DiagnosticResult).run(
        unittest.defaultTestLoader.discover(str(ROOT / 'worker'), pattern=args.pattern))
    after = dict(self_maintenance._protected_snapshot(ROOT).entries)
    changes = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    source_after = source_snapshot(ROOT)
    source_changes = sorted(k for k in source_before.keys() | source_after.keys() if source_before.get(k) != source_after.get(k))
    evidence = {'engine_tree_changes': source_changes, 'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
                'skipped': len(result.skipped), 'protected_path_changes': changes}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(evidence))
    raise SystemExit(0 if result.wasSuccessful() and not changes and not source_changes else 1)

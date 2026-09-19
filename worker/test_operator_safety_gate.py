import tempfile
import unittest
from pathlib import Path

import operator_safety_gate as gate


class OperatorSafetyGateTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_current_designated_operator_sources_pass(self):
        self.assertEqual(gate.audit(self.ROOT), ())

    def test_exact_incident_patterns_and_direct_subprocess_are_rejected(self):
        cases = {
            "Get-ChildItem X:\\synthetic\\c\\root -Recurse": "recursive-enumeration",
            "Get-Content big.log -Raw": "unbounded-raw-read",
            "Start-Process powershell -Verb RunAs": "adhoc-elevation",
            "Get-CimInstance Win32_Process | Select ProcessId": "unfiltered-process-snapshot",
            "$rows += $item": "powershell-array-append",
            "taskkill /IM powershell.exe": "image-name-cleanup",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                violations = gate._powershell_text_violations(source, "test.ps1")
                self.assertIn(expected, {item.rule for item in violations})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.py"
            path.write_text("import subprocess\nsubprocess.run(['x'])\n", encoding="utf-8")
            violations = gate._python_violations(path, "bad.py")
            self.assertIn("direct-subprocess-outside-boundary", {item.rule for item in violations})

    def test_filtered_projected_process_query_is_allowed(self):
        value = "Get-CimInstance Win32_Process -Filter \"ProcessId=$pid\""
        self.assertEqual(gate._powershell_text_violations(value, "safe.ps1"), [])


if __name__ == "__main__":
    unittest.main()

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = REPO_ROOT / "protocol" / "v2" / "check_conformance.py"

spec = importlib.util.spec_from_file_location(
    "bridge_protocol_v2_conformance_for_test",
    CHECKER_PATH,
)
if spec is None or spec.loader is None:
    raise RuntimeError("Unable to load Protocol-v2 conformance checker for tests")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)


def canonical_command(meta: dict) -> str:
    payload = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    return f"<!-- bridge-command: {payload} -->\n# Command\n"


def superseder_meta() -> dict:
    return {
        "command_id": 2,
        "source": "manual_chatgpt",
        "based_on_report": 0,
        "expected_generation": 2,
        "kind": "EXECUTE",
        "supersedes_command_id": 1,
    }


def withdrawal_replacement_meta(**updates) -> dict:
    value = {
        "command_id": 2,
        "source": "manual_chatgpt",
        "based_on_report": 0,
        "expected_generation": 2,
        "kind": "EXECUTE",
        "manual_request_id": "request-2",
        "withdraws_command_id": 1,
    }
    value.update(updates)
    return value


class SupersedePhysicalHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-conformance-physical-history-")
        self.root = Path(self.temp.name)
        self.project = self.root / "projects" / "p"
        (self.project / "commands").mkdir(parents=True)
        self.original_root = cc.ROOT
        cc.ROOT = self.root

    def tearDown(self):
        cc.ROOT = self.original_root
        self.temp.cleanup()

    def validate(self):
        return cc.validate_history(
            self.project,
            command_sources=set(cc.protocol_core.COMMAND_SOURCES),
            command_kinds=set(cc.protocol_core.COMMAND_KINDS),
            owner_statuses=set(),
            verification_statuses=set(),
            canonical_latest_command=2,
            warnings=[],
        )

    def write_superseder(self):
        (self.project / "commands" / "command-002.md").write_text(
            canonical_command(superseder_meta()),
            encoding="utf-8",
        )

    def test_superseder_accepts_existing_invalid_target_without_canonical_marker(self):
        # Exercise the case where immutable command bytes exist, but the
        # historical command used a Markdown JSON fence rather than the
        # canonical Protocol-v2 HTML metadata marker.
        (self.project / "commands" / "command-001.md").write_text(
            "# Historical invalid command\n\n"
            "```json\n"
            '{"command_id":1,"source":"manual_chatgpt","based_on_report":0,'
            '"expected_generation":1,"kind":"EXECUTE"}\n'
            "```\n",
            encoding="utf-8",
        )
        self.write_superseder()

        self.assertEqual(self.validate(), [])

    def test_superseder_still_rejects_physically_missing_target(self):
        self.write_superseder()

        errors = self.validate()
        self.assertTrue(
            any(
                "supersedes_command_id references missing command-001" in error
                for error in errors
            ),
            errors,
        )

    def test_withdrawal_relation_accepts_an_earlier_existing_target(self):
        (self.project / "commands" / "command-001.md").write_text(
            canonical_command(
                {
                    "command_id": 1,
                    "source": "manual_chatgpt",
                    "based_on_report": 0,
                    "expected_generation": 1,
                    "kind": "EXECUTE",
                }
            ),
            encoding="utf-8",
        )
        (self.project / "commands" / "command-002.md").write_text(
            canonical_command(withdrawal_replacement_meta()),
            encoding="utf-8",
        )
        self.assertEqual(self.validate(), [])

    def test_withdrawal_relation_rejects_self_and_future_targets(self):
        (self.project / "commands" / "command-001.md").write_text(
            canonical_command(
                {
                    "command_id": 1,
                    "source": "manual_chatgpt",
                    "based_on_report": 0,
                    "expected_generation": 1,
                    "kind": "EXECUTE",
                    "withdraws_command_id": 1,
                }
            ),
            encoding="utf-8",
        )
        errors = self.validate()
        self.assertTrue(any("withdraws_command_id must not self-reference" in error for error in errors), errors)

        (self.project / "commands" / "command-001.md").write_text(
            canonical_command(
                {
                    "command_id": 1,
                    "source": "manual_chatgpt",
                    "based_on_report": 0,
                    "expected_generation": 1,
                    "kind": "EXECUTE",
                    "withdraws_command_id": 2,
                }
            ),
            encoding="utf-8",
        )
        errors = self.validate()
        self.assertTrue(
            any("withdraws_command_id must refer to an earlier command id" in error for error in errors),
            errors,
        )


if __name__ == "__main__":
    unittest.main()

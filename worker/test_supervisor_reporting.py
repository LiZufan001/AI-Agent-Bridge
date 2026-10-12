"""Reporting-contract regression, not proof of a real model's output quality."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "templates/supervisor-hourly-report.zh-CN.md"


class SupervisorReportingContractTests(unittest.TestCase):
    def test_all_output_routes_share_the_template(self):
        self.assertTrue((ROOT / TEMPLATE).is_file())
        for path in (
            "SUPERVISOR_ENTRYPOINT.md",
            "policies/portfolio-attention.md",
            "docs/automation/bridge-portfolio-supervisor-current-prompt.md",
        ):
            with self.subTest(path=path):
                self.assertIn(TEMPLATE, (ROOT / path).read_text(encoding="utf-8"))

    def test_skeleton_is_chinese_and_not_an_internal_field_dump(self):
        text = (ROOT / TEMPLATE).read_text(encoding="utf-8")
        skeleton = text.split("```markdown\n", 1)[1].split("```", 1)[0]
        for heading in ("本轮结论", "本轮进展", "项目概况", "下一步与需要你处理的事"):
            self.assertIn(heading, skeleton)
        for raw in ("rotation_index", "worker_available", "expected_generation", "deferred-this-pass"):
            self.assertNotIn(raw, skeleton)

    def test_stage_translations_keep_evidence_strength_distinct(self):
        text = (ROOT / TEMPLATE).read_text(encoding="utf-8")
        rows = {}
        for line in text.splitlines():
            if line.startswith("| "):
                cells = [s.strip() for s in line.strip("|").split("|")]
                rows[cells[0]] = cells[1:]
        expected = {
            "STAGED_INPUT_SUBMITTED": "尚未确认接收",
            "COMMAND_READY": "等待开始",
            "REPORT_READY": "等待审阅",
            "RECOVERY_REQUIRED": "暂不重跑",
            "deferred-this-pass": "本轮未展开",
        }
        for key, wording in expected.items():
            with self.subTest(key=key):
                self.assertIn(wording, rows[key][0])
        self.assertIn("不等于项目", rows["REPORT_READY"][1])
        self.assertIn("目标级验收", rows["SUCCESS"][1])

    def test_live_prompt_preserves_execution_boundary_and_chinese_fallback(self):
        text = (ROOT / "docs/automation/bridge-portfolio-supervisor-current-prompt.md").read_text(encoding="utf-8")
        execution, output = text.split("OUTPUT IN CHINESE\n", 1)
        for contract in ("AUTOMATION SELF-CONTROL:", "RECOVERY PREEMPTION:",
                         "expected_generation = G + 1", "exactly ONE focus project",
                         "Owner execution mode `auto`", "unattended adoption remains disabled"):
            self.assertIn(contract, execution)
        self.assertIn("STAGED_INPUT_SUBMITTED", output)
        self.assertIn("模板读取失败", output)
        self.assertIn("非重点项目", output)
        self.assertIn("前后证据", output)
        self.assertIn("Do not wait for Worker/Codex completion", output)
        self.assertNotIn("Report compactly:\n- heartbeat", output)


if __name__ == "__main__":
    unittest.main()

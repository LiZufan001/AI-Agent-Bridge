"""Pure planning and request identity tests; no production or model calls."""
import json
import unittest
from datetime import datetime, timezone

import supervisor_tools as helpers
from supervisor_publication_gateway_core import StagedPublicationRequest


def state_bytes(**changes):
    value = {'protocol_version': 2, 'project_id': 'p', 'status': 'REPORT_READY',
             'generation': 10, 'latest_command': 5, 'latest_report': 5,
             'last_reviewed_report': 4, 'active_run': None,
             'finalized': False, 'human_required': False}
    value.update(changes)
    return json.dumps(value).encode('utf-8')


class FixedRingTests(unittest.TestCase):
    @staticmethod
    def projects():
        return [{'project_id': key, 'priority_rank': index,
                 'owner_selected': True, 'owner_paused': False}
                for index, key in enumerate('ABC')]

    def test_dynamic_eligibility_keeps_always_eligible_anchor(self):
        choices, legacy = [], []
        for hour in range(12):
            eligible = list('ABC') if hour % 3 != 1 else (list('AB') if hour % 2 == 0 else list('BC'))
            decision = helpers.choose_focus(self.projects(), eligible, hour_bucket=hour)
            choices.append(decision['focus_project'])
            legacy.append(eligible[hour % len(eligible)])
        self.assertEqual(legacy.count('B'), 0)
        self.assertEqual(choices.count('B'), 4)

    def test_paused_members_remain_anchor_but_never_selected(self):
        projects = self.projects()
        projects[1]['owner_paused'] = True
        result = helpers.choose_focus(projects, list('ABC'), hour_bucket=1)
        self.assertEqual(result['owner_ring'], list('ABC'))
        self.assertEqual(result['anchor_index'], 1)
        self.assertEqual(result['focus_project'], 'C')

    def test_empty_all_ineligible_and_order_independence(self):
        self.assertIsNone(helpers.choose_focus([], [], hour_bucket=0)['focus_project'])
        self.assertIsNone(helpers.choose_focus(self.projects(), [], hour_bucket=1)['focus_project'])
        self.assertEqual(helpers.choose_focus(self.projects(), list('ABC'), hour_bucket=7),
                         helpers.choose_focus(list(reversed(self.projects())), list('ABC'), hour_bucket=7))

    def test_invalid_and_unjustified_candidates_rejected(self):
        with self.assertRaises(helpers.PreparationError):
            helpers.choose_focus(self.projects() * 2, ['A'], hour_bucket=0)
        with self.assertRaises(helpers.PreparationError):
            helpers.choose_focus(self.projects(), ['D'], hour_bucket=0)
        with self.assertRaises(helpers.PreparationError):
            helpers.choose_focus(self.projects(), ['A'], hour_bucket=True)
        with self.assertRaises(helpers.PreparationError):
            helpers.choose_focus(self.projects(), ['A'], hour_bucket=0, preempt_project='B')


class RequestPreparationTests(unittest.TestCase):
    def test_exact_bytes_ids_and_default_profile_inheritance(self):
        body = '# Task\r\n\r\n验证中文🙂 and ordinary tests.\r\n'
        prepared = helpers.prepare_request(state_bytes(), body, producer='scheduled_chatgpt',
            request_id='synthetic', now=datetime(2020, 1, 1, tzinfo=timezone.utc))
        parsed = StagedPublicationRequest.from_bytes(prepared.request_bytes, filename_request_id='synthetic')
        self.assertEqual(parsed.command_id, 6)
        self.assertEqual(parsed.expected_generation, 11)
        self.assertEqual(parsed.based_on_report, 5)
        self.assertTrue(parsed.command_bytes.endswith(body.encode('utf-8')))
        self.assertNotIn('executor', parsed.metadata)
        self.assertEqual(prepared.filename, 'request-synthetic.json')
        self.assertFalse(prepared.summary()['authorization_verified'])

    def test_immutable_input_finalizer_and_override(self):
        raw = state_bytes()
        prepared = helpers.prepare_request(raw, 'Final checks', producer='finalizer',
            executor={'model': 'synthetic-model', 'reasoning_effort': 'high'})
        parsed = StagedPublicationRequest.from_bytes(prepared.request_bytes, filename_request_id=prepared.request_id)
        self.assertEqual(parsed.kind, 'FINALIZE')
        self.assertEqual(parsed.metadata['executor']['reasoning_effort'], 'high')
        self.assertNotIn('executor', json.loads(prepared.request_bytes))
        self.assertEqual(raw, state_bytes())

    def test_ineligible_or_duplicate_identity_input_is_rejected(self):
        cases = [(state_bytes(status='CODEX_RUNNING'), 'body'),
                 (state_bytes(active_run={'run_id': 'x'}), 'body'),
                 (state_bytes(human_required=True), 'body'),
                 (state_bytes(generation=True), 'body'),
                 (state_bytes(), '<!-- bridge-command: {} -->')]
        for raw, body in cases:
            with self.subTest(body=body[:12]), self.assertRaises(ValueError):
                helpers.prepare_request(raw, body, producer='scheduled_chatgpt')
        with self.assertRaises(helpers.PreparationError):
            helpers.prepare_request(state_bytes(), 'body', producer='manual_chatgpt')
        with self.assertRaises(helpers.PreparationError):
            helpers.prepare_request(state_bytes(), 'body', producer='scheduled_chatgpt', now=datetime(2020, 1, 1))

    def test_recovery_advisory_never_grants_retry(self):
        state = json.loads(state_bytes(status='RECOVERY_REQUIRED'))
        journal = {'project_id': 'p', 'command_id': 5, 'run_id': 'run-test',
                   'claim_generation': 9, 'external_side_effects_unknown': True}
        result = helpers.recovery_advisory(state, journal)
        self.assertEqual(result['classification'], 'external_effects_require_review')
        self.assertFalse(result['automatic_retry_allowed'])
        self.assertFalse(result['canonical_state_changed'])
        self.assertEqual(helpers.recovery_advisory(state, None)['classification'], 'evidence_required')


if __name__ == '__main__':
    unittest.main()

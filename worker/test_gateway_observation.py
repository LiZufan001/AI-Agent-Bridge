"""Inbox diagnostics are observations, not permission or new commands."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from supervisor_publication_gateway_core import MAX_SCAN_ENTRIES, SupervisorPublicationGateway


class GatewayObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-observation-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / 'worker/staged-publications/requests'
        self.inbox.mkdir(parents=True)
        self.gateway = SupervisorPublicationGateway(self.root)

    def test_noise_does_not_hide_129th_valid_filename(self):
        for index in range(MAX_SCAN_ENTRIES):
            (self.inbox / f'noise-{index:03d}.txt').write_text('synthetic')
        (self.inbox / 'request-late.json').write_text('{}')
        paths = self.gateway._request_paths()
        self.assertEqual([key for _, key in paths], ['late'])
        self.assertEqual(self.gateway.last_scan['ignored_entry_count'], MAX_SCAN_ENTRIES)
        self.assertTrue(self.gateway.last_scan['complete'])

    def test_io_failure_not_silent_empty_and_unsupported_names_not_mutated(self):
        with patch('supervisor_publication_gateway_core.os.scandir', side_effect=PermissionError):
            result = self.gateway.poll()
        self.assertEqual(result[0].outcome, 'scan_diagnostic')
        self.assertEqual(result[0].reason, 'scan_permission_denied')
        old = self.inbox / 'legacy-name.json'
        old.write_text('{}')
        result = self.gateway.poll()
        self.assertEqual(result[0].reason, 'unsupported_request_filename')
        self.assertEqual(old.read_text(), '{}')

    def test_hard_budget_is_explicit_and_case_is_consistent(self):
        class Entry:
            name = 'noise.txt'
            path = 'not-read'
        class Scan:
            def __enter__(self): return iter([Entry()] * (MAX_SCAN_ENTRIES * 32 + 1))
            def __exit__(self, *_): return False
        with patch('supervisor_publication_gateway_core.os.scandir', return_value=Scan()):
            self.assertEqual(self.gateway._request_paths(), ())
        self.assertFalse(self.gateway.last_scan['complete'])
        self.assertEqual(self.gateway.last_scan['status'], 'scan_incomplete')
        (self.inbox / 'REQUEST-upper.JSON').write_text('{}')
        self.assertEqual(self.gateway._request_paths()[0][1], 'upper')

    def test_cursor_visits_more_than_a_page_without_persistent_state(self):
        count = MAX_SCAN_ENTRIES + 9
        for index in range(count):
            (self.inbox / f'request-{index:03d}.json').write_text('{}')
        seen = set()
        rounds = (count + self.gateway.max_requests_per_poll - 1) // self.gateway.max_requests_per_poll
        for _ in range(rounds):
            seen.update(key for _, key in self.gateway._request_paths())
        self.assertEqual(len(seen), count)
        self.assertEqual(len(list(self.inbox.iterdir())), count)

    def test_poll_visits_every_candidate_even_when_earlier_inputs_never_retire(self):
        from supervisor_publication_gateway_core import StagedPublicationError
        count = MAX_SCAN_ENTRIES * 2
        for index in range(count):
            (self.inbox / f'request-{index:03d}.json').write_text('{}')
        seen = set()
        def unavailable(path):
            seen.add(path.name)
            raise StagedPublicationError('request_not_tracked')
        rounds = (count + self.gateway.max_requests_per_poll - 1) // self.gateway.max_requests_per_poll
        with patch.object(self.gateway, '_read_staged_blob', side_effect=unavailable):
            for _ in range(rounds):
                results = self.gateway.poll()
                self.assertLessEqual(len(results), self.gateway.max_requests_per_poll)
        self.assertEqual(len(seen), count)
        self.assertEqual(len(list(self.inbox.iterdir())), count)

    def test_uncertain_publication_is_not_retired(self):
        from supervisor_publication_gateway_core import _RETIREABLE_OUTCOMES
        self.assertNotIn('error_safe', _RETIREABLE_OUTCOMES)


if __name__ == '__main__':
    unittest.main()

import ast
import json
import logging
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

import requests
import test_recovery
from src import database, reporting, source_recovery, work_queue as q


class LateSourceTests(unittest.TestCase):
    setUp = test_recovery.RecoveryTests.setUp
    rows = [dict(start_time=99., end_time=111., segment_size=1.)]

    def close_missing(self):
        with patch.object(q.time, 'time', return_value=10000):
            q.failed('test-event', 'recordings_missing', missing=True)
        with patch.object(q.time, 'time', return_value=13601):
            q.failed('test-event', 'recordings_missing', missing=True)

    def job(self):
        with q.connection() as c:
            return dict(c.execute('SELECT * FROM upload_jobs WHERE event_id="test-event"').fetchone())

    def marker(self):
        with q.connection() as c:
            return dict(c.execute('SELECT * FROM events WHERE event_id="test-event"').fetchone())

    def check(self, rows=None, now=20000):
        with patch.object(q.time, 'time', return_value=now), patch.object(
                source_recovery.segmented_upload, 'recordings', return_value=self.rows if rows is None else rows) as fetch:
            result = source_recovery.recheck_one('http://source.invalid')
            return result, fetch

    def test_source_appearing_two_days_later_returns_to_normal_queue(self):
        self.close_missing(); before = self.job()
        result, fetch = self.check(now=200000)
        self.assertEqual(result, 'available')
        fetch.assert_called_once_with('http://source.invalid', 'camera', 100., 110., timeout=(5, 15))
        job = self.job()
        self.assertEqual(job['state'], 'pending')
        self.assertEqual(job['late_source_result'], 'reopened')
        self.assertEqual(job['late_source_reopened_at'], 200000)
        for key in ('failures', 'last_failure_at', 'metadata'):
            self.assertEqual(job[key], before[key])
        self.assertIsNone(job['missing_since'])
        self.assertEqual(self.marker()['retry'], 1)
        self.assertEqual(self.marker()['uploaded'], 0)
        self.assertEqual(q.parts('test-event'), [])
        self.assertEqual(self.check()[0], None)

    def test_absent_source_preserves_history_and_marker(self):
        self.close_missing(); before = self.job(); marker = self.marker()
        self.assertEqual(self.check(rows=[])[0], 'missing')
        after = self.job()
        for key in before:
            if not key.startswith('late_source_'):
                self.assertEqual(after[key], before[key], key)
        self.assertEqual(self.marker(), marker)
        self.assertEqual(after['late_source_next_check'], 23600)

    def test_persistent_schedule_survives_restart_and_rediscovery(self):
        self.close_missing(); self.check(rows=[])
        q.initialize(); q.enqueue(self.event)
        with patch.object(q.time, 'time', return_value=23599):
            self.assertIsNone(q.claim_missing_source())
        with patch.object(q.time, 'time', return_value=23600):
            self.assertIsNotNone(q.claim_missing_source())

    def test_claim_is_exclusive_and_one_job_per_call(self):
        self.close_missing()
        with patch.object(q.time, 'time', return_value=20000):
            job = q.claim_missing_source()
            self.assertIsNotNone(job)
            self.assertIsNone(q.claim_missing_source())
        self.assertEqual(self.job()['late_source_checks'], 1)

    def test_network_or_http_failure_is_not_new_missing_evidence(self):
        self.close_missing(); before = self.job(); marker = self.marker()
        for error in (requests.Timeout('private'), requests.ConnectionError('private'), requests.HTTPError('private')):
            with self.subTest(error=type(error).__name__), patch.object(q.time, 'time', return_value=200000+len(str(error))*3600), patch.object(
                    source_recovery.segmented_upload, 'recordings', side_effect=error):
                with q.connection() as c:
                    c.execute('UPDATE upload_jobs SET late_source_next_check=0')
                self.assertEqual(source_recovery.recheck_one('http://source.invalid'), 'request_failed')
                self.assertEqual(self.job()['missing_since'], before['missing_since'])
                self.assertEqual(self.job()['failures'], before['failures'])
                self.assertEqual(self.marker(), marker)

    def test_invalid_metadata_does_not_access_source(self):
        self.close_missing()
        for value in ('bad-json', 'null', json.dumps(dict(self.event, id='another')), json.dumps(dict(self.event, end_time=float('nan')))):
            with self.subTest(value=value), q.connection() as c:
                c.execute('UPDATE upload_jobs SET metadata=?,late_source_next_check=0', (value,))
            result, fetch = self.check()
            self.assertEqual(result, 'invalid_metadata'); fetch.assert_not_called()
        self.assertEqual(self.job()['state'], 'unavailable')

    def test_nonoverlap_empty_or_invalid_intervals_never_reopen(self):
        self.close_missing()
        for rows, expected in (([], 'missing'), ([dict(start_time=200., end_time=210., segment_size=1)], 'missing'),
                ([dict(start_time=100., end_time=float('inf'), segment_size=1)], 'invalid_source'),
                ([dict(start_time=110., end_time=100., segment_size=1)], 'invalid_source'),
                ([dict(start_time=100., end_time=110., segment_size=0)], 'invalid_source')):
            with self.subTest(rows=rows), q.connection() as c:
                c.execute('UPDATE upload_jobs SET late_source_next_check=0')
            self.assertEqual(self.check(rows=rows)[0], expected)
            self.assertEqual(self.job()['state'], 'unavailable')

    def test_new_success_during_request_cannot_be_reset(self):
        self.close_missing(); claim = q.claim_missing_source(); q.complete('test-event')
        before = self.job(); marker = self.marker()
        self.assertFalse(q.finish_source_check(claim, 'available'))
        self.assertEqual(self.job(), before); self.assertEqual(self.marker(), marker)

    def test_new_metadata_during_request_requires_fresh_check(self):
        self.close_missing(); claim = q.claim_missing_source()
        q.enqueue(dict(self.event, end_time=120.)); before = self.job()
        self.assertFalse(q.finish_source_check(claim, 'available'))
        self.assertEqual(self.job(), before)

    def test_any_existing_part_or_assigned_identity_blocks_reopen(self):
        self.close_missing(); claim = q.claim_missing_source()
        q.save_plan('test-event', [dict(start=100, end=110, duration=10)])
        q.update_part('test-event', 0, drive_id='saved-identity', size=1, md5='saved-digest')
        before = q.parts('test-event')
        self.assertFalse(q.finish_source_check(claim, 'available'))
        self.assertIsNone(q.claim_missing_source())
        self.assertEqual(q.parts('test-event'), before)

    def test_partial_review_complete_pending_and_other_errors_are_excluded(self):
        self.close_missing()
        for state, error in [('partial', 'recordings_missing'), ('review', 'recordings_missing'),
                ('complete', 'recordings_missing'), ('pending', 'recordings_missing'),
                ('unavailable', 'source_gaps_confirmed'), ('unavailable', 'event_metadata_missing')]:
            with self.subTest(state=state, error=error), q.connection() as c:
                c.execute('UPDATE upload_jobs SET state=?,error=?', (state, error))
            before = self.job()
            result, fetch = self.check()
            self.assertIsNone(result); fetch.assert_not_called(); self.assertEqual(self.job(), before)

    def test_failed_probe_can_later_recover_and_no_new_uploads_are_marked_successful(self):
        self.close_missing(); self.check(rows=[])
        self.assertEqual(self.check(now=23600)[0], 'available')
        self.assertEqual(self.marker()['uploaded'], 0)
        stats = reporting.collect(self.db, now=23601)['late_sources']
        self.assertEqual(stats['checks'], 2); self.assertEqual(stats['reopened'], 1)
        self.assertEqual(stats['last_results'], [('reopened', 1)])

    def test_worker_processes_uploads_before_single_check(self):
        tree = ast.parse((Path(__file__).parents[1]/'main.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'upload_worker')
        order = []
        queue = Mock(); queue.due.return_value = [dict(event_id='test-event', metadata=json.dumps(self.event))]
        import threading
        drive = types.SimpleNamespace(upload_lock=threading.Lock())
        worker_time = Mock(); worker_time.monotonic.return_value = 10.
        ready = Mock(); ready.wait.side_effect = StopIteration
        recovery = Mock(); recovery.recheck_one.side_effect = lambda _: order.append('check')
        seg = Mock(); seg.step.side_effect = lambda *args: order.append('upload')
        ns = dict(json=json, logging=logging, time=worker_time, work_queue=queue,
            google_drive=drive, segmented_upload=seg, source_recovery=recovery,
            FRIGATE_URL='http://source.invalid', _work_ready=ready)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'main.py', 'exec'), ns)
        with self.assertRaises(StopIteration): ns['upload_worker']()
        self.assertEqual(order, ['upload', 'check']); recovery.recheck_one.assert_called_once()


if __name__ == '__main__':
    unittest.main()

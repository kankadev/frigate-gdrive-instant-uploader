import ast
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

import requests
from src import database, work_queue as q, segmented_upload as seg, frigate_api, mattermost_handler


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name)/'events.db')
        self.dbpatch = patch.object(database, 'DB_PATH', self.db)
        self.dbpatch.start()
        self.addCleanup(self.dbpatch.stop)
        database.init_db(self.db)
        with sqlite3.connect(self.db) as c:
            c.execute('ALTER TABLE events ADD COLUMN last_error_kind TEXT')
        q.initialize()
        self.event = dict(id='test-event', camera='camera', label='person', start_time=100., end_time=110., has_clip=True)
        q.enqueue(self.event)

    def test_outage_never_exhausts_retry_budget(self):
        for _ in range(100):
            q.failed('test-event', 'network')
        with q.connection() as c:
            r = c.execute('SELECT * FROM upload_jobs').fetchone()
            self.assertEqual(r['state'], 'pending')
            self.assertEqual(r['failures'], 100)
            self.assertLessEqual(r['next_attempt']-time.time(), 3600)
        q.initialize()
        self.assertEqual(database.select_retry('test-event', self.db), 1)

    def test_missing_requires_separate_confirmations(self):
        with patch.object(q.time, 'time', return_value=10000):
            q.failed('test-event', 'source_missing', missing=True)
        with patch.object(q.time, 'time', return_value=10060):
            q.failed('test-event', 'source_missing', missing=True)
        self.assertEqual(database.select_retry('test-event', self.db), 1)
        with patch.object(q.time, 'time', return_value=13601):
            q.failed('test-event', 'source_missing', missing=True)
        self.assertEqual(database.select_retry('test-event', self.db), 0)
        q.enqueue(self.event)
        q.initialize()
        self.assertEqual(database.select_retry('test-event', self.db), 0)
        self.assertTrue(database.is_event_exists('test-event', self.db))

    def test_network_error_cannot_confirm_missing(self):
        with patch.object(q.time, 'time', return_value=10000):
            q.failed('test-event', 'source_missing', missing=True)
        q.failed('test-event', 'network')
        with patch.object(q.time, 'time', return_value=20000):
            q.failed('test-event', 'source_missing', missing=True)
        self.assertEqual(database.select_retry('test-event', self.db), 1)

    def test_cleanup_preserves_unresolved_work(self):
        with q.connection() as c:
            c.execute("UPDATE events SET created='2000-01-01'")
        database.cleanup_old_events(self.db)
        self.assertTrue(database.is_event_exists('test-event', self.db))
        self.assertEqual(len(q.due()), 1)

    def test_existing_upload_not_queued(self):
        q.complete('test-event')
        q.enqueue(self.event)
        self.assertEqual(q.due(), [])

    def test_historical_unavailable_is_separate_from_retry_backlog(self):
        with q.connection() as c:
            c.execute("UPDATE events SET created='2000-01-01',retry=0,last_error_kind='recordings_missing'")
        stats=database.get_health_stats(self.db)
        self.assertEqual(stats['pending_non_retryable'],1)
        self.assertEqual(stats['pending_retryable'],0)
        self.assertEqual(stats['pending_gt_3d'],0)

    def test_backoff_survives_rediscovery(self):
        q.failed('test-event', 'network')
        q.enqueue(self.event)
        self.assertEqual(q.due(), [])

    def test_plan_preserves_recordings_and_skips_gaps(self):
        rows = [dict(start_time=i*10, end_time=i*10+10, segment_size=6.4) for i in range(100)]
        rows += [dict(start_time=68000, end_time=68010, segment_size=6.4)]
        plan = seg.make_plan(rows, 2, 69000)
        self.assertAlmostEqual(sum(p['duration'] for p in plan), 1008)
        self.assertTrue(all(p['end']-p['start'] <= 300 for p in plan))
        self.assertEqual(plan[0]['start'], 2)
        self.assertEqual(plan[-1]['end'], 68010)

    def test_source_server_error_is_not_missing(self):
        response = Mock(status_code=500)
        response.raise_for_status.side_effect = requests.HTTPError('server error')
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        path = Path(self.temp.name)/'clip.mp4'
        with patch.object(seg.requests, 'get', return_value=response):
            with self.assertRaises(requests.HTTPError):
                seg.download('http://test', self.event, dict(start=100,end=110,duration=10), path)
        self.assertFalse(path.exists())

    def test_lost_upload_response_recovers_by_id_without_create(self):
        q.save_plan('test-event', [dict(start=100,end=110,duration=10)])
        q.update_part('test-event', 0, drive_id='known-id', size=100, md5='known-hash')
        drive = Mock()
        drive.service.files.return_value.get.return_value.execute.return_value = dict(id='known-id',size='100',md5Checksum='known-hash',trashed=False)
        seg.step(self.event, 'http://test', drive)
        self.assertEqual(database.select_event_uploaded('test-event', self.db), 1)
        drive.service.files.return_value.create.assert_not_called()

    def test_wrong_remote_bytes_do_not_mark_uploaded(self):
        p = dict(drive_id='known-id', size=100, md5='known-hash')
        service = Mock()
        service.files.return_value.get.return_value.execute.return_value = dict(size='99',md5Checksum='known-hash')
        with self.assertRaises(ValueError):
            seg.verified_remote(service, p)

    def test_damaged_part_does_not_block_other_existing_parts(self):
        q.save_plan('test-event', [dict(start=100,end=105,duration=5),dict(start=105,end=110,duration=5)])
        q.update_part('test-event', 1, drive_id='known-id', size=100, md5='known-hash')
        drive = Mock()
        drive.service.files.return_value.get.return_value.execute.return_value = dict(id='known-id',size='100',md5Checksum='known-hash',trashed=False)
        with patch.object(seg, 'download', side_effect=ValueError('truncated')):
            seg.step(self.event, 'http://test', drive)
        self.assertEqual([p['uploaded'] for p in q.parts('test-event')],[0,1])
        self.assertEqual(database.select_event_uploaded('test-event',self.db),0)

    def test_mattermost_failure_does_not_recurse(self):
        handler = mattermost_handler.MattermostHandler('http://invalid')
        with patch.object(mattermost_handler.requests, 'post', side_effect=requests.Timeout) as post:
            handler.emit(logging.LogRecord('test',logging.ERROR,'',0,'message',(),None))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['timeout'], 10)

    def test_http_pagination_retains_lower_bound(self):
        responses = [Mock(status_code=200), Mock(status_code=200), Mock(status_code=200)]
        responses[0].json.return_value = [dict(start_time=20)]
        responses[1].json.return_value = [dict(start_time=15)]
        responses[2].json.return_value = []
        with patch.object(frigate_api.requests, 'get', side_effect=responses) as get:
            events = frigate_api.fetch_all_events('http://test',after=10,batch_size=1)
        self.assertEqual(len(events),2)
        self.assertTrue(all(c.kwargs['params']['after']==10 for c in get.call_args_list))

    def test_mqtt_callback_only_queues_and_survives_bad_json(self):
        tree = ast.parse((Path(__file__).parents[1]/'main.py').read_text())
        fn = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='on_message')
        ns = dict(json=json,logging=logging,work_queue=q,_work_ready=threading.Event())
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'main.py','exec'),ns)
        ns['on_message'](None,None,types.SimpleNamespace(payload=b'broken'))
        ns['on_message'](None,None,types.SimpleNamespace(payload=json.dumps(dict(type='end',after=self.event)).encode()))
        self.assertTrue(ns['_work_ready'].is_set())


if __name__ == '__main__':
    unittest.main()

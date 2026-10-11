import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import Mock, patch

import requests
import test_recovery
from src import database, work_queue as q, segmented_upload as seg, reporting


class RepairTests(unittest.TestCase):
    setUp = test_recovery.RecoveryTests.setUp

    def probe(self, payload, expected=1):
        with patch.object(seg.subprocess, 'run', return_value=Mock(returncode=0, stdout=json.dumps(payload))):
            return seg.validate_video('unused', expected)

    def test_probe_without_duration_is_classified_not_keyerror(self):
        with self.assertRaises(seg.MediaError) as ex:
            self.probe({'format': {}, 'streams': []})
        self.assertEqual(ex.exception.kind, 'media_duration_missing')

    def test_nonfinite_or_negative_duration_rejected(self):
        for value in ['nan', 'inf', '-1', '0']:
            with self.subTest(value=value), self.assertRaises(seg.MediaError):
                self.probe({'format': {'duration': value}, 'streams': [{'codec_type':'video'}]})

    def test_audio_only_is_not_success(self):
        with self.assertRaises(seg.MediaError) as ex:
            self.probe({'format': {'duration':'10'}, 'streams': [{'codec_type':'audio'}]})
        self.assertEqual(ex.exception.kind, 'media_video_missing')

    def test_tiny_video_does_not_pass_longer_subsecond_expectation(self):
        with self.assertRaises(seg.MediaError):
            self.probe({'format': {'duration':'.1'}, 'streams': [{'codec_type':'video'}]}, .9)

    def test_real_ffprobe_and_video_stream(self):
        path = Path(self.temp.name)/'synthetic.mp4'
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi',
            '-i','color=c=black:s=64x64:r=10','-t','2','-c:v','mpeg4',str(path)], check=True, timeout=30)
        self.assertGreaterEqual(seg.validate_video(path,2),1.99)
        with self.assertRaises(seg.MediaError):
            seg.validate_video(path,10)

    def response(self):
        r = Mock(status_code=200, headers={})
        r.__enter__ = Mock(return_value=r)
        r.__exit__ = Mock(return_value=False)
        r.iter_content.return_value = [b'synthetic-video']
        return r

    def test_subsecond_download_uses_bounded_expansion(self):
        q.save_plan('test-event',[dict(start=100.1,end=100.9,duration=.8)])
        part = q.parts('test-event')[0]
        rows = [dict(start_time=100.,end_time=110.,segment_size=1)]
        with patch.object(seg,'recordings',return_value=rows), patch.object(seg.requests,'get',return_value=self.response()) as get, patch.object(seg,'validate_video',return_value=3):
            seg.download('http://source',self.event,part,Path(self.temp.name)/'clip.mp4')
        self.assertIn('/start/99/end/102/',get.call_args.args[0])
        stored = q.parts('test-event')[0]
        self.assertEqual(stored['duration'],.8)
        self.assertEqual(stored['coverage'],'full')

    def test_shrunk_http200_saves_remaining_media_without_rewriting_plan(self):
        q.save_plan('test-event',[dict(start=100,end=200,duration=100)])
        rows=[dict(start_time=100,end_time=110,segment_size=1)]
        path=Path(self.temp.name)/'clip.mp4'
        with patch.object(seg,'recordings',return_value=rows), patch.object(seg.requests,'get',return_value=self.response()), patch.object(seg,'validate_video',return_value=10), patch.object(seg.time,'time',return_value=10000):
            seg.download('http://source',self.event,q.parts('test-event')[0],path)
        self.assertTrue(path.exists())
        part=q.parts('test-event')[0]
        self.assertEqual(part['duration'],100)
        self.assertEqual(part['coverage'],'partial_pending')
        self.assertEqual(json.loads(part['source_intervals']),[[100,110]])
        self.assertEqual(part['source_missing_since'],10000)

    def test_shrunk_source_never_reuses_assigned_identity_for_other_bytes(self):
        q.save_plan('test-event',[dict(start=100,end=200,duration=100)])
        q.update_part('test-event',0,drive_id='already-assigned',md5='old-md5',size=123)
        with patch.object(seg,'recordings',return_value=[dict(start_time=100,end_time=110,segment_size=1)]), self.assertRaises(seg.MediaError) as ex:
            seg.download('http://source',self.event,q.parts('test-event')[0],Path(self.temp.name)/'clip.mp4')
        self.assertEqual(ex.exception.kind,'recovery_identity_conflict')
        self.assertEqual(q.parts('test-event')[0]['drive_id'],'already-assigned')

    def prepare_partial(self):
        q.save_plan('test-event',[dict(start=100,end=200,duration=100)])
        q.update_part('test-event',0,uploaded=1,drive_id='saved',size=100,md5='sum',
            coverage='partial_pending',source_intervals='[[100,110]]',source_missing_since=10000,source_checked_at=10000)

    def test_partial_requires_hour_then_finishes_as_partial_not_success(self):
        self.prepare_partial()
        rows=[dict(start_time=100,end_time=110,segment_size=1)]
        with patch.object(seg,'recordings',return_value=rows), patch.object(seg.time,'time',return_value=10061):
            self.assertFalse(seg.confirm_coverage(self.event,'http://source',q.parts('test-event')[0]))
        with patch.object(seg,'recordings',return_value=rows), patch.object(seg.time,'time',return_value=13601):
            seg.step(self.event,'http://source',Mock())
        with q.connection() as c:
            self.assertEqual(c.execute('select state from upload_jobs').fetchone()[0],'partial')
        self.assertEqual(database.select_event_uploaded('test-event',self.db),0)
        self.assertEqual(q.parts('test-event')[0]['drive_id'],'saved')

    def test_source_check_failure_cannot_confirm_gap(self):
        self.prepare_partial()
        with patch.object(seg,'recordings',side_effect=requests.Timeout), patch.object(seg.time,'time',return_value=20000), self.assertRaises(requests.Timeout):
            seg.confirm_coverage(self.event,'http://source',q.parts('test-event')[0])
        self.assertEqual(q.parts('test-event')[0]['coverage'],'partial_pending')

    def test_reappearing_source_is_visible_and_preserves_rescued_bytes(self):
        self.prepare_partial()
        with patch.object(seg,'recordings',return_value=[dict(start_time=100,end_time=120,segment_size=1)]), patch.object(seg.time,'time',return_value=20000), self.assertRaises(seg.MediaError) as ex:
            seg.confirm_coverage(self.event,'http://source',q.parts('test-event')[0])
        self.assertEqual(ex.exception.kind,'source_coverage_changed')
        self.assertEqual(q.parts('test-event')[0]['drive_id'],'saved')

    def test_migration_idempotent_and_existing_successes_unchanged(self):
        self.prepare_partial()
        before=q.parts('test-event')
        q.initialize();q.initialize()
        self.assertEqual(before,q.parts('test-event'))

    def test_deterministic_failure_enters_review_and_is_not_rediscovered(self):
        for _ in range(3):q.failed('test-event','media_probe_failed',detail='safe',deterministic=True)
        q.enqueue(self.event);q.initialize()
        self.assertEqual(q.due(),[])
        with q.connection() as c:self.assertEqual(c.execute('select state from upload_jobs').fetchone()[0],'review')

    def test_transient_failures_do_not_exhaust_retry_budget(self):
        for _ in range(12):q.failed('test-event','source_timeout')
        with q.connection() as c:self.assertEqual(c.execute('select state from upload_jobs').fetchone()[0],'pending')

    def test_classification_does_not_leak_external_exception_text(self):
        kind,detail,deterministic=reporting.classify_error(requests.Timeout('sensitive-url'))
        self.assertEqual(kind,'source_timeout');self.assertFalse(deterministic)
        self.assertNotIn('sensitive-url',detail)

    def test_historical_gaps_do_not_raise_current_alarm(self):
        self.prepare_partial();q.finish_with_gaps('test-event')
        s=reporting.collect(self.db,now=100000000000)
        title,text,color,ok=reporting.render(s,dict(db=True,mqtt=True,upload_worker=True))
        self.assertTrue(ok);self.assertEqual(s['active'],0)
        self.assertEqual(s['historical'],1);self.assertEqual(s['historical_parts'],1)
        self.assertNotIn('source_gaps_confirmed',text)

    def test_worker_failure_is_critical_even_without_backlog(self):
        q.complete('test-event')
        title,_,_,ok=reporting.render(reporting.collect(self.db),dict(upload_worker=False))
        self.assertFalse(ok);self.assertIn('CRITICAL',title)

    def test_newly_confirmed_gaps_are_informational(self):
        self.prepare_partial();q.finish_with_gaps('test-event')
        stats=reporting.collect(self.db)
        self.assertEqual(stats['new_gaps_24h'],1)
        title,text,color,ok=reporting.render(stats,dict(upload_worker=True))
        self.assertTrue(ok)
        self.assertIn('Operational',title)
        self.assertEqual(color,'#36a64f')
        self.assertIn('Historical source gaps – information only',text)
        self.assertIn('Newly closed with gaps in the last 24 hours: **1**',text)
        self.assertIn('do not require action',text)

    def test_historical_gaps_do_not_hide_current_problems(self):
        self.prepare_partial();q.finish_with_gaps('test-event')
        stats=reporting.collect(self.db)
        cases=[({'review':1},True,'Action required','#ffae42'),
               ({'active_errors':[('source_timeout',1)]},True,'Action required','#ffae42'),
               ({},False,'CRITICAL','#d50000'),
               ({'active':1,'gap_checks':0,'uploaded_24h':0},True,'CRITICAL','#d50000')]
        for changes,healthy,label,expected_color in cases:
            with self.subTest(changes=changes,healthy=healthy):
                title,_,color,ok=reporting.render({**stats,**changes},dict(upload_worker=healthy))
                self.assertFalse(ok)
                self.assertIn(label,title)
                self.assertEqual(color,expected_color)

    def test_missing_database_stats_raise_instead_of_zero_success(self):
        empty=str(Path(self.temp.name)/'empty.db')
        sqlite3.connect(empty).close()
        with self.assertRaises(sqlite3.DatabaseError):reporting.collect(empty)
        with self.assertRaises(sqlite3.DatabaseError):database.get_health_stats(empty)

    def test_partial_restart_never_reuploads_confirmed_file(self):
        self.prepare_partial();q.initialize()
        with patch.object(seg.time,'time',return_value=10030), patch.object(seg,'download') as download, patch.object(seg,'verified_remote') as remote:
            seg.step(self.event,'http://source',Mock())
        download.assert_not_called();remote.assert_not_called()

    def test_overlap_union_is_not_double_counted(self):
        spans=seg.intervals([dict(start_time=100,end_time=110),dict(start_time=105,end_time=115)],100,115)
        self.assertEqual(seg.interval_duration(spans),15)
        self.assertEqual(seg.uncovered([[100,115]],[[100,110]]),5)

    def test_new_plans_preserve_original_intervals(self):
        plan=seg.make_plan([dict(start_time=100,end_time=110,segment_size=1),dict(start_time=120,end_time=130,segment_size=1)],100,130)
        q.save_plan('test-event',plan)
        self.assertEqual(json.loads(q.parts('test-event')[0]['planned_intervals']),[[100,110],[120,130]])

    def test_missing_source_that_returns_is_retried(self):
        q.save_plan('test-event',[dict(start=100,end=110,duration=10)])
        q.update_part('test-event',0,coverage='missing_pending',source_intervals='[]',source_missing_since=10000,source_checked_at=10000)
        with patch.object(seg,'recordings',return_value=[dict(start_time=100,end_time=110,segment_size=1)]),patch.object(seg.time,'time',return_value=10100):
            self.assertFalse(seg.confirm_coverage(self.event,'http://source',q.parts('test-event')[0]))
        self.assertEqual(q.parts('test-event')[0]['coverage'],'full')

    def test_partial_upload_is_verified_but_event_is_not_complete(self):
        q.save_plan('test-event',[dict(start=100,end=200,duration=100)])
        def download(base,event,part,path):
            path.write_bytes(b'rescued')
            q.update_part(event['id'],part['part'],coverage='partial_pending',source_intervals='[[100,110]]',
                source_missing_since=10000,source_checked_at=10000,archived_duration=10)
        drive=Mock()
        drive.service.files.return_value.generateIds.return_value.execute.return_value={'ids':['new-id']}
        drive.generate_filename.return_value='2026-01-01__event.mp4'
        drive.find_or_create_folder.return_value='folder'
        drive.service.files.return_value.create.return_value.next_chunk.return_value=(None,{'id':'new-id'})
        with patch.object(seg,'download',side_effect=download),patch.object(seg,'validate_video',return_value=10),patch.object(seg,'verified_remote',side_effect=[False,True]),patch.object(seg,'MediaFileUpload'):
            seg.step(self.event,'http://source',drive)
        part=q.parts('test-event')[0]
        self.assertEqual(part['uploaded'],1);self.assertEqual(part['drive_id'],'new-id')
        self.assertEqual(part['duration'],100);self.assertEqual(part['coverage'],'partial_pending')
        self.assertEqual(database.select_event_uploaded('test-event',self.db),0)

    def test_report_never_calls_clip_availability_and_includes_worker(self):
        import ast,types
        tree=ast.parse((Path(__file__).parents[1]/'main.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='daily_health_report')
        send=Mock()
        ns=dict(reporting=reporting,database=database,logging=Mock(),
            _get_subsystem_status=lambda _:dict(db=True,mqtt=True,mqtt_status='connected',scheduler=True,upload_worker=False),
            check_frigate_reachable=lambda _:True,FRIGATE_URL='http://source',
            HEALTH_REPORT_ONLY_ON_ISSUES=False,send_mattermost_notification=send)
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'main.py','exec'),ns)
        ns['daily_health_report'](None)
        self.assertIn('CRITICAL',send.call_args.kwargs['title'])
        self.assertNotIn('mqtt_status',send.call_args.kwargs['text'])

    def test_public_report_uses_english_in_all_severity_branches(self):
        q.complete('test-event')
        stats=reporting.collect(self.db)
        for changes,systems,label in [({},dict(upload_worker=True),'Operational'),
                ({'review':1},dict(upload_worker=True),'Action required'),
                ({},dict(upload_worker=False),'CRITICAL')]:
            with self.subTest(label=label):
                title,text,_,_=reporting.render({**stats,**changes},systems)
                self.assertIn(label,title)
                self.assertIn('Current operations',text)
                self.assertIn('Historical source gaps',text)
                self.assertIn('Subsystem status',text)
                for previous in ['Aktueller','Historische','Ereignisse','PRÜFEN','Handlungsbedarf']:
                    self.assertNotIn(previous,title+text)

    def test_report_statistics_failure_notification_uses_english(self):
        import ast
        tree=ast.parse((Path(__file__).parents[1]/'main.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='daily_health_report')
        send=Mock();reports=Mock();reports.collect.side_effect=sqlite3.DatabaseError('unavailable')
        ns=dict(reporting=reports,database=database,logging=Mock(),
            HEALTH_REPORT_ONLY_ON_ISSUES=True,send_mattermost_notification=send)
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'main.py','exec'),ns)
        ns['daily_health_report'](None)
        self.assertIn('Status check failed',send.call_args.kwargs['title'])
        self.assertIn('Status is unknown',send.call_args.kwargs['text'])

"""Evidence regression tests: only temporary files, mocked pings and timestamp data."""
import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

import activity_history as h
import keepalive as k
import settings as s


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'XDG_CONFIG_HOME': self.temp.name})
        self.env.start()
        self.settings = s.Settings()
        self.issue_epoch = 2000000000.0
        self.issued = h.utc_time(self.issue_epoch)
        self.attempt = str(uuid.uuid4())
        self.session = Path(self.temp.name)/'sync-webdav/profiles/default/session.json'
        self.session.parent.mkdir(parents=True)
        self.session.write_text(json.dumps({'issuedAt': self.issued,'email':'private@example.test','authToken':'super-secret'}))
        self.session.chmod(0o600)

    def tearDown(self):
        self.env.stop();self.temp.cleanup()

    def event(self, event, seconds, attempt=None):
        epoch = self.issue_epoch + seconds
        return {'event':event,'epoch':epoch,'at':h.utc_time(epoch),'attempt':attempt or self.attempt,
                'session_issued_at':self.issued,'interval_seconds':60}

    def snapshot(self, expired=True):
        return Mock(auth_reason='expired' if expired else '', session_issued_at=self.issued,
                    webdav=Mock(active_exit_monotonic=9990*1000000,running=not expired))

    def test_session_only_issued_timestamp_is_exposed(self):
        self.assertEqual(h.session_issued_at(self.settings),self.issued)
        h.append(self.event('success',60))
        data=h.history_path().read_text()
        self.assertNotIn('super-secret',data);self.assertNotIn('private@example',data)
        self.assertEqual(h.history_path().stat().st_mode & 0o777,0o600)
        self.assertEqual(h.history_path().parent.stat().st_mode & 0o777,0o700)

    def test_unvalidated_fields_and_dates_are_never_logged(self):
        bad=self.event('success',60);bad['password']='secret'
        with self.assertRaises(ValueError):h.append(bad)
        bad=self.event('success',60);bad['at']='secret-token'
        with self.assertRaises(ValueError):h.append(bad)
        bad=self.event('success',60);bad['attempt']='filename/path'
        with self.assertRaises(ValueError):h.append(bad)
        bad=self.event('success',60);bad['epoch']=float('nan')
        with self.assertRaises(ValueError):h.append(bad)

    def test_rotation_retains_one_bounded_previous_file(self):
        with patch.object(h,'MAX_BYTES',1024):
            for i in range(25):h.append(self.event('success',i,attempt=str(uuid.uuid4())))
            path=h.history_path();backup=path.with_suffix('.jsonl.1')
            self.assertTrue(backup.exists())
            self.assertLessEqual(path.stat().st_size,1024)
            self.assertLessEqual(backup.stat().st_size,1024)
            self.assertEqual(backup.stat().st_mode & 0o777,0o600)
            self.assertLess(len(h.read_history()),25)

    def test_unknown_or_malformed_records_not_displayed(self):
        h.append(self.event('success',60))
        with h.history_path().open('a') as stream:stream.write('{broken\n'+json.dumps({'event':'success','token':'leak'})+'\n')
        records=h.read_history();self.assertEqual(len(records),1)

    def test_cache_reuses_parsed_history_but_keeps_session_and_clock_live(self):
        h.append(self.event('success', 60))
        with patch.object(h, '_decoded_history', (None, ())), patch.object(h, '_decode', wraps=h._decode) as decode:
            with patch('time.time', return_value=self.issue_epoch + 100) as clock:
                report = h.report(self.settings, self.snapshot(expired=False))
                self.assertEqual(report['session_age_seconds'], 100)
                self.assertEqual(report['successful_reads'], 1)
                clock.return_value += 20
                report = h.report(self.settings, self.snapshot(expired=False))
                self.assertEqual(report['session_age_seconds'], 120)
                self.assertEqual(decode.call_count, 2)  # current + rotated, once
                h.append(self.event('success', 110))
                self.assertEqual(h.report(self.settings, self.snapshot(expired=False))['successful_reads'], 2)
                self.assertEqual(decode.call_count, 4)
                self.session.write_text(json.dumps({'issuedAt': h.utc_time(self.issue_epoch + 118)}))
                report = h.report(self.settings, self.snapshot(expired=False))
                self.assertEqual(report['session_age_seconds'], 2)
                self.assertEqual(report['successful_reads'], 0)
                self.assertEqual(decode.call_count, 4)

    def test_symlinks_and_public_history_fail_closed_even_after_cache_is_warm(self):
        h.append(self.event('success', 60))
        path = h.history_path()
        self.assertEqual(len(h.read_history()), 1)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            h.read_history()
        target = path.with_name('regular-history')
        path.rename(target)
        target.chmod(0o600)
        path.symlink_to(target)  # Same bytes as the cached history.
        with self.assertRaises(ValueError):
            h.read_history()
        with self.assertRaises(ValueError):
            h.append(self.event('success', 120))

    def test_expiry_is_deduplicated_and_survives_restart(self):
        with patch('time.time',return_value=self.issue_epoch+3700),patch('time.monotonic',return_value=10090):
            h.observe_expiry(self.settings,self.snapshot())
            h.observe_expiry(self.settings,self.snapshot())
        with patch('time.time',return_value=self.issue_epoch+3700.9),patch('time.monotonic',return_value=10090):
            h.observe_expiry(self.settings,self.snapshot())
        self.assertEqual(len(h.read_history()),1)
        self.assertEqual(h.read_history()[0]['event'],'session_expired')
        with patch('time.time',return_value=self.issue_epoch+4000):
            report=h.report(self.settings,self.snapshot(expired=False))
        self.assertEqual(report['session_age_seconds'],3600)
        self.assertIn('not the configured timeout',report['summary'])

    def test_old_snapshot_cannot_record_or_report_expiry_for_new_login(self):
        self.session.write_text(json.dumps({'issuedAt':h.utc_time(self.issue_epoch+5000)}))
        with patch('time.time',return_value=self.issue_epoch+6000),patch('time.monotonic',return_value=12390):
            h.observe_expiry(self.settings,self.snapshot())
            self.assertEqual(h.read_history(),[])
            report=h.report(self.settings,self.snapshot())
        self.assertEqual(report['server_exit_at'],'')
        self.assertIn('Current session age',report['summary'])

    def test_incomplete_or_inconsistent_expiry_records_rejected(self):
        record=self.event('session_expired',3600)
        with self.assertRaises(ValueError):h.append(record)
        record.update(server_exit_at=h.utc_time(self.issue_epoch+3600),server_exit_epoch=self.issue_epoch+3650,
                      server_exit_monotonic=9990*1000000,boot_id='00000000-0000-0000-0000-000000000000')
        with self.assertRaises(ValueError):h.append(record)

    def test_summary_uses_only_same_session_successes(self):
        for secs,event in [(60,'started'),(61,'success'),(120,'started'),(121,'success'),(3590,'started'),(3591,'success'),(3650,'failed')]:
            h.append(self.event(event,secs,attempt=str(uuid.uuid4())))
        unrelated=self.event('success',1);unrelated['session_issued_at']=h.utc_time(self.issue_epoch-1000);h.append(unrelated)
        with patch('time.time',return_value=self.issue_epoch+3700),patch('time.monotonic',return_value=10090):
            report=h.report(self.settings,self.snapshot())
        self.assertEqual(report['successful_reads'],3)
        self.assertEqual(report['failed_reads'],1)
        self.assertEqual(report['session_age_seconds'],3600)
        self.assertIn('0h 0m 9s',report['summary'])
        self.assertIn('Largest gap',report['summary'])
        self.assertNotIn('super-secret',report['summary']+report['history'])

    def test_historical_absence_is_explicit_not_assumed_success(self):
        with patch('time.time',return_value=self.issue_epoch+3700),patch('time.monotonic',return_value=10090):
            report=h.report(self.settings,self.snapshot())
        self.assertEqual(report['successful_reads'],0)
        self.assertIn('No successful reads recorded',report['summary'])

    def test_ping_start_result_and_duration_recorded_without_output(self):
        settings=replace(self.settings,rclone_config=str(Path(self.temp.name)/'rclone.conf'))
        with patch.object(k,'operation_lock',return_value=contextlib.nullcontext()),patch.object(k,'ensure_local_remote'), \
             patch.object(k,'read_remote'):
            result=k.ping(settings,threading.Event(),status_reader=lambda _:Mock(state='online',stale_mount=False))
        records=h.read_history();self.assertEqual([r['event'] for r in records],['started','success'])
        self.assertEqual(records[0]['attempt'],records[1]['attempt'])
        self.assertEqual(records[0]['session_issued_at'],self.issued)
        self.assertIn('duration_seconds',records[1])
        self.assertEqual(result.history_error,'')

    def test_previous_expiry_report_remains_when_new_session_is_saved(self):
        with patch('time.time',return_value=self.issue_epoch+3700),patch('time.monotonic',return_value=10090):
            h.observe_expiry(self.settings,self.snapshot())
        self.session.write_text(json.dumps({'issuedAt':h.utc_time(self.issue_epoch+5000),'authToken':'do-not-print'}))
        with patch('time.time',return_value=self.issue_epoch+5100):
            report=h.report(self.settings,self.snapshot(expired=False))
        self.assertIn('Previous recorded session expiries',report['history'])
        self.assertIn('1h 0m 0s',report['history'])
        self.assertEqual(report['successful_reads'],0)

    def test_history_write_failure_does_not_fail_successful_read(self):
        with patch.object(k,'operation_lock',return_value=contextlib.nullcontext()),patch.object(k,'ensure_local_remote'), \
             patch.object(k,'read_remote'),patch.object(h,'record_attempt',side_effect=OSError('disk full')):
            result=k.ping(self.settings,threading.Event(),status_reader=lambda _:Mock(state='online',stale_mount=False))
        self.assertEqual(result.state,'success');self.assertIn('could not be written',result.history_error)


if __name__=='__main__':unittest.main()

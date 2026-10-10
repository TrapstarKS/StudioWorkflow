import json
import sqlite3
import time
import unittest
from contextlib import closing, contextmanager

import test_autosync as fixtures
from studioworkflow.bridge import Workspace, digest, read_text


class TransportRecoveryTest(unittest.TestCase):
    setUp = fixtures.AutoSyncTest.setUp
    record = fixtures.AutoSyncTest.record
    post = fixtures.AutoSyncTest.post
    snapshot = fixtures.AutoSyncTest.snapshot
    local = fixtures.AutoSyncTest.local
    tick = fixtures.AutoSyncTest.tick
    jobs = fixtures.AutoSyncTest.jobs
    sync_state = fixtures.AutoSyncTest.sync_state
    restart = fixtures.AutoSyncTest.restart

    def post_v2(self, route, **payload):
        return self.post(route, **{"protocol_version": 2, "epoch": 0, **payload})

    def snapshot_v2(self, records=None, epoch=0, token='snapshot-1', session='a'):
        records = [self.record()] if records is None else records
        envelope = dict(protocol_version=2, epoch=epoch, snapshot_id=token, session=session)
        self.post('/snapshot/start', count=len(records), **envelope)
        self.post('/snapshot/batch', scripts=records, **envelope)
        return self.post('/snapshot/finish', **envelope)

    def operation_row(self, operation):
        with self.workspace.connect() as connection:
            return dict(connection.execute('SELECT * FROM operations WHERE id=?', (operation['id'],)).fetchone())

    def session_row(self):
        with self.workspace.connect() as connection:
            return dict(connection.execute("SELECT * FROM sessions WHERE place='main'").fetchone())

    def base(self):
        with self.workspace.connect() as connection:
            return connection.execute('SELECT source FROM file_bases WHERE place=? AND path=?', ('main', self.path)).fetchone()['source']

    def delivered(self, source='return 2\n'):
        self.local(source)
        operation = self.workspace.queue_apply('main', self.path)
        self.post('/heartbeat', operation_chunks=True)
        return operation

    def test_global_pause_survives_restart_and_blocks_all_outbound_paths(self):
        baseline = 'a' * 310000
        self.snapshot([self.record(baseline)])
        operation = self.delivered(baseline + '\n')
        self.workspace.set_outbound_paused(True, 'mirror reconstruction')
        self.restart()
        self.assertTrue(self.workspace.status()['main']['outbound_paused'])
        self.assertEqual(self.post('/heartbeat')['operations'], [])
        with self.assertRaisesRegex(ValueError, 'paused'):
            self.workspace.queue_apply('main', self.path)
        self.assertEqual(self.workspace.queue_apply('main', self.path, automatic=True)['state'], 'paused')
        with self.assertRaisesRegex(ValueError, 'paused'):
            self.post('/operation/chunk', id=operation['id'], position=1)
        self.post('/changes', scripts=[self.record(baseline + '\n')])
        self.assertEqual(self.operation_row(operation)['state'], 'applied')
        self.assertTrue(self.post('/heartbeat')['outbound_paused'])

    def test_pause_between_queue_and_heartbeat_hides_queued_source(self):
        self.snapshot()
        self.local('return 2\n')
        operation = self.workspace.queue_apply('main', self.path)
        peer = Workspace(self.root)
        peer.set_outbound_paused(True)
        self.assertEqual(self.post('/heartbeat')['operations'], [])
        self.assertEqual(self.operation_row(operation)['state'], 'queued')
        peer.set_outbound_paused(False)
        self.assertEqual(self.post('/heartbeat')['operations'][0]['id'], operation['id'])

    def test_per_place_pause_is_preserved_when_global_pause_clears(self):
        self.snapshot()
        self.snapshot(place=2)
        self.workspace.set_outbound_paused(True, place='main')
        self.workspace.set_outbound_paused(True)
        self.workspace.set_outbound_paused(False)
        self.assertTrue(self.post('/heartbeat')['outbound_paused'])
        self.assertFalse(self.post('/heartbeat', place=2)['outbound_paused'])

    def test_safe_reset_requires_pause_and_preserves_uncertain_delivery(self):
        self.snapshot()
        operation = self.delivered()
        with self.assertRaisesRegex(ValueError, 'Pause'):
            self.workspace.invalidate_sessions()
        self.workspace.set_outbound_paused(True)
        self.workspace.invalidate_sessions(reason='rebuild')
        self.assertEqual(self.session_row()['complete'], 0)
        self.assertEqual(self.operation_row(operation)['state'], 'uncertain')
        self.snapshot([self.record('return 2\n')])
        self.assertEqual(self.operation_row(operation)['state'], 'applied')
        self.assertTrue(self.post('/heartbeat')['outbound_paused'])

    def test_protocol_gate_blocks_old_plugin_even_with_ready_snapshot(self):
        self.snapshot()
        operation = self.delivered()
        self.workspace.config['minimum_observer_protocol'] = 2
        self.assertEqual(self.workspace.status()['main']['state'], 'restart_required')
        self.assertTrue(self.post('/heartbeat')['restart_required'])
        with self.assertRaisesRegex(ValueError, 'protocol'):
            self.workspace.queue_apply('main', self.path)
        with self.assertRaisesRegex(ValueError, 'protocol'):
            self.post('/operation/chunk', id=operation['id'], position=1)
        # Merely advertising a higher version cannot promote an old inventory.
        self.assertTrue(self.post_v2('/heartbeat')['restart_required'])
        self.snapshot_v2([self.record('return 2\n')])
        self.assertFalse(self.post_v2('/heartbeat')['restart_required'])

    def test_offline_heartbeat_cannot_revive_stale_inventory(self):
        self.snapshot()
        self.local('return 2\n')
        operation = self.workspace.queue_apply('main', self.path)
        with self.workspace.connect() as connection:
            connection.execute("UPDATE sessions SET heartbeat=heartbeat-21 WHERE place='main'")
        with self.assertRaisesRegex(ValueError, 'new complete snapshot'):
            self.post('/heartbeat')
        self.assertEqual(self.session_row()['complete'], 0)
        self.assertEqual(self.operation_row(operation)['state'], 'expired')
        with self.assertRaises(ValueError):
            self.workspace.queue_apply('main', self.path)

    def test_heartbeat_incomplete_hint_is_a_persistent_barrier(self):
        self.snapshot_v2()
        self.local('return 2\n')
        self.workspace.queue_apply('main', self.path)
        response = self.post_v2('/heartbeat', snapshot_incomplete=True)
        self.assertTrue(response['snapshot_required'])
        self.assertEqual(response['operations'], [])
        self.restart()
        self.assertFalse(self.post_v2('/heartbeat')['complete'])
        with self.assertRaises(ValueError):
            self.post_v2('/snapshot/finish', snapshot_id='snapshot-1')

    def test_stale_epoch_and_snapshot_token_cannot_mutate_inventory(self):
        self.snapshot_v2(epoch=4, token='new')
        before = self.session_row()
        with self.assertRaisesRegex(ValueError, 'epoch'):
            self.post('/changes', protocol_version=2, epoch=3, scripts=[self.record('stale')])
        self.assertEqual(self.session_row(), before)
        self.post('/snapshot/start', protocol_version=2, epoch=5, snapshot_id='next', count=1)
        with self.assertRaisesRegex(ValueError, 'snapshot identity'):
            self.post('/snapshot/batch', protocol_version=2, epoch=5, snapshot_id='new', scripts=[self.record('stale')])
        self.assertEqual(read_text(self.workspace.work_file('main', self.path)), 'return 1\n')

    def test_snapshot_start_and_finish_retries_are_idempotent(self):
        payload = dict(protocol_version=2, epoch=0, snapshot_id='retry')
        self.post('/snapshot/start', count=1, **payload)
        self.post('/snapshot/batch', scripts=[self.record()], **payload)
        self.post('/snapshot/start', count=1, **payload)
        self.assertEqual(self.post('/snapshot/finish', **payload)['scripts'], 1)
        self.assertEqual(self.post('/snapshot/finish', **payload)['scripts'], 1)
        self.assertTrue(self.post('/snapshot/start', count=1, **payload)['complete'])

    def test_lost_ack_echo_advances_base_without_losing_newer_local_save(self):
        self.snapshot()
        operation = self.delivered()
        self.local('return 3\n')
        self.tick()
        self.post('/changes', scripts=[self.record('return 2\n')])
        self.assertEqual(self.operation_row(operation)['state'], 'applied')
        self.assertEqual(self.base(), 'return 2\n')
        self.tick(3)
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(self.jobs()[-1]['baseline'], 'return 2\n')
        self.assertEqual(self.jobs()[-1]['source'], 'return 3\n')

    def test_ack_before_echo_holds_next_write_until_observation(self):
        self.snapshot()
        operation = self.delivered()
        self.local('return 3\n')
        self.post('/operation/result', id=operation['id'], outcome='applied')
        self.assertEqual(self.base(), 'return 2\n')
        self.tick()
        self.tick(3)
        self.assertEqual(len(self.jobs()), 1)
        self.post('/changes', scripts=[self.record('return 2\n')])
        self.tick(3)
        self.assertEqual(self.jobs()[-1]['baseline'], 'return 2\n')

    def test_applied_history_cannot_rebase_a_later_team_create_edit(self):
        self.snapshot()
        operation = self.delivered()
        self.post('/changes', scripts=[self.record('return 2\n')])
        self.post('/operation/result', id=operation['id'], outcome='applied')
        self.post('/changes', scripts=[self.record('return 1\n')])
        self.local('return 3\n')
        self.post('/changes', scripts=[self.record('return 2\n')])
        self.assertEqual(self.base(), 'return 1\n')
        with self.assertRaisesRegex(ValueError, 'Studio changed'):
            self.workspace.queue_apply('main', self.path)

    def test_foreign_identity_echo_does_not_confirm_delivered_operation(self):
        self.snapshot()
        operation = self.delivered()
        self.local('return 3\n')
        foreign = self.record('return 2\n')
        foreign['metadata']['instance_id'] = 'replacement'
        self.post('/changes', scripts=[foreign])
        self.assertEqual(self.operation_row(operation)['state'], 'delivered')
        self.assertEqual(self.base(), 'return 1\n')

    def test_same_content_replacement_identity_preserves_dirty_base_guard(self):
        self.snapshot()
        self.local('return 2\n')
        foreign = self.record()
        foreign['metadata']['instance_id'] = 'replacement'
        self.post('/changes', scripts=[foreign])
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.workspace.queue_apply('main', self.path)
        self.tick()
        self.tick(3)
        self.assertEqual(self.sync_state()['state'], 'blocked')
        self.assertEqual(self.jobs(), [])

    def test_uncertain_delivery_blocks_only_its_file_and_is_not_replayed(self):
        other = 'ReplicatedStorage/Other.luau'
        self.snapshot([self.record(), self.record(path=other)])
        operation = self.delivered()
        self.workspace.work_file('main', other).write_text('return 9\n')
        self.tick()
        self.tick(35)
        self.assertEqual(self.operation_row(operation)['state'], 'uncertain')
        self.assertEqual([job['path'] for job in self.jobs()], [self.path, other])
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            self.workspace.resume('main', self.path)
        self.snapshot([self.record(), self.record(path=other)])
        self.tick(3)
        self.assertEqual(sum(job['path'] == self.path for job in self.jobs()), 1)

    def test_delivery_timeout_starts_at_delivery_and_tracks_chunk_activity(self):
        baseline = 'a' * 310000
        self.snapshot([self.record(baseline)])
        self.local(baseline + '\n')
        operation = self.workspace.queue_apply('main', self.path)
        with self.workspace.connect() as connection:
            connection.execute('UPDATE operations SET created=created-29 WHERE id=?', (operation['id'],))
        self.post('/heartbeat', operation_chunks=True)
        self.tick(5)
        self.assertEqual(self.operation_row(operation)['state'], 'delivered')
        with self.workspace.connect() as connection:
            connection.execute('UPDATE operations SET activity_at=? WHERE id=?', (time.time() - 29, operation['id']))
        self.post('/operation/chunk', id=operation['id'], position=1)
        self.tick(5)
        self.assertEqual(self.operation_row(operation)['state'], 'delivered')

    def test_current_epoch_ack_settles_old_epoch_delivery_after_restart(self):
        self.snapshot_v2()
        operation = self.delivered()
        self.local('return 3\n')
        self.snapshot_v2([self.record('return 2\n')], epoch=1, token='new')
        self.restart()
        payload = dict(protocol_version=2, epoch=1, id=operation['id'], outcome='applied')
        self.assertTrue(self.post('/operation/result', **payload)['ok'])
        self.assertTrue(self.post('/operation/result', **payload)['ok'])
        self.assertEqual(self.base(), 'return 2\n')
        self.assertEqual(len(self.jobs()), 1)

    def test_late_ack_without_echo_is_accepted_after_same_epoch_snapshot(self):
        self.snapshot_v2()
        operation = self.delivered()
        self.local('return 3\n')
        self.snapshot_v2(epoch=0, token='resnapshot')
        self.assertEqual(self.operation_row(operation)['state'], 'uncertain')
        self.post_v2('/operation/result', epoch=0, id=operation['id'], outcome='applied')
        self.assertEqual(self.operation_row(operation)['state'], 'applied')
        self.assertEqual(self.base(), 'return 2\n')
        self.tick()
        self.tick(3)
        self.assertEqual(len(self.jobs()), 1)
        self.post_v2('/changes', epoch=0, scripts=[self.record('return 2\n')])
        self.tick(3)
        self.assertEqual(self.jobs()[-1]['baseline'], 'return 2\n')

    def test_new_epoch_inventory_recovers_lost_heartbeat_response(self):
        self.snapshot_v2()
        operation = self.delivered()
        self.snapshot_v2(epoch=1, token='after-network-interruption')
        self.assertEqual(self.operation_row(operation)['state'], 'expired')
        self.assertEqual(json.loads(self.operation_row(operation)['result'])['delivery'], 'epoch_fenced')
        result = self.post_v2('/operation/result', epoch=1, id=operation['id'], outcome='applied')
        self.assertTrue(result['terminal'])
        self.assertEqual(self.base(), 'return 1\n')
        self.tick()
        self.tick(3)
        self.assertEqual(len(self.jobs()), 2)
        self.assertNotEqual(self.jobs()[-1]['id'], operation['id'])
        self.assertEqual(self.jobs()[-1]['baseline'], 'return 1\n')
        self.assertEqual(self.jobs()[-1]['epoch'], 1)

    def test_same_epoch_inventory_does_not_assume_missing_write_never_executed(self):
        self.snapshot_v2()
        operation = self.delivered()
        self.snapshot_v2(token='same-epoch-resnapshot')
        self.assertEqual(self.operation_row(operation)['state'], 'uncertain')
        self.tick()
        self.tick(3)
        self.assertEqual(len(self.jobs()), 1)

    def test_new_epoch_does_not_retry_over_changed_studio_source(self):
        self.snapshot_v2()
        operation = self.delivered()
        self.snapshot_v2([self.record('return "collaborator"\n')], epoch=1, token='concurrent-change')
        self.assertEqual(self.operation_row(operation)['state'], 'uncertain')
        self.tick()
        self.tick(3)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(read_text(self.workspace.work_file('main', self.path)), 'return 2\n')

    def test_new_epoch_does_not_retry_against_replacement_identity(self):
        self.snapshot_v2()
        operation = self.delivered()
        replacement = self.record()
        replacement['metadata']['instance_id'] = 'different-instance'
        self.snapshot_v2([replacement], epoch=1, token='replacement')
        self.assertEqual(self.operation_row(operation)['state'], 'uncertain')
        self.tick()
        self.tick(3)
        self.assertEqual(len(self.jobs()), 1)

    def test_new_session_exact_local_match_closes_obsolete_uncertainty(self):
        self.snapshot_v2()
        operation = self.delivered()
        self.post_v2('/disconnect')
        replacement = self.record('return 2\n')
        replacement['metadata']['instance_id'] = 'new-observer-identity'
        self.snapshot_v2([replacement], session='reopened', token='reopened')
        self.assertEqual(self.operation_row(operation)['state'], 'superseded')
        self.local('return 3\n')
        next_operation = self.workspace.queue_apply('main', self.path)
        self.assertNotEqual(next_operation['id'], operation['id'])
        self.assertEqual(self.operation_row(next_operation)['baseline'], 'return 2\n')

    def test_new_session_preserves_uncertainty_when_local_differs(self):
        self.snapshot_v2()
        operation = self.delivered()
        self.post_v2('/disconnect')
        replacement = self.record()
        replacement['metadata']['instance_id'] = 'new-observer-identity'
        self.snapshot_v2([replacement], session='reopened', token='reopened')
        self.assertEqual(self.operation_row(operation)['state'], 'uncertain')
        with self.assertRaises(ValueError):
            self.workspace.queue_apply('main', self.path)

    def test_result_with_missing_session_or_operation_is_terminal(self):
        response = self.post_v2('/operation/result', id='missing', outcome='applied')
        self.assertTrue(response['terminal'])
        self.assertTrue(response['snapshot_required'])
        self.snapshot_v2()
        self.assertTrue(self.post_v2('/operation/result', id='missing', outcome='applied')['terminal'])

    def test_contradictory_result_is_terminal_without_reversing_confirmation(self):
        self.snapshot()
        operation = self.delivered()
        self.post('/changes', scripts=[self.record('return 2\n')])
        response = self.post('/operation/result', id=operation['id'], outcome='rejected')
        self.assertTrue(response['terminal'])
        self.assertTrue(response['snapshot_required'])
        self.assertEqual(self.operation_row(operation)['state'], 'applied')
        self.assertEqual(self.session_row()['complete'], 0)

    def test_known_never_delivered_operation_cannot_be_resurrected_by_result(self):
        self.snapshot()
        self.local('return 2\n')
        operation = self.workspace.queue_apply('main', self.path)
        self.snapshot()
        self.assertTrue(self.post('/operation/result', id=operation['id'], outcome='applied')['terminal'])
        self.assertEqual(self.operation_row(operation)['state'], 'expired')
        self.assertEqual(self.base(), 'return 1\n')

    def test_temporary_readiness_error_does_not_stay_blocked(self):
        self.snapshot()
        self.local('return 2\n')
        with self.workspace.connect() as connection:
            connection.execute('INSERT INTO autosync VALUES(?,?,?,?,?,?,?)', (
                'main', self.path, digest('return 2\n'), self.now - 3, 'blocked',
                'Studio is offline, playing or still refreshing; writes are blocked', None))
        self.tick()
        self.assertEqual(len(self.jobs()), 1)

    def test_safe_temporary_rejection_recovers_only_after_fresh_snapshot(self):
        self.snapshot_v2()
        operation = self.delivered()
        self.post_v2('/operation/result', id=operation['id'], outcome='rejected',
                     details={'code': 'play_mode', 'write_attempted': False})
        self.tick()
        self.tick(3)
        self.assertEqual(len(self.jobs()), 1)
        self.snapshot_v2(token='after-rejection')
        self.tick(3)
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(self.jobs()[-1]['baseline'], 'return 1\n')

    def test_temporary_rejection_never_retries_over_team_create_change(self):
        self.snapshot_v2()
        operation = self.delivered()
        self.post_v2('/operation/result', id=operation['id'], outcome='rejected',
                     details={'code': 'play_mode', 'write_attempted': False})
        self.snapshot_v2([self.record('return "collaborator"\n')], token='changed')
        self.tick()
        self.tick(3)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.sync_state()['state'], 'blocked')

    def test_ack_without_echo_requests_fresh_snapshot_but_does_not_replay(self):
        self.snapshot_v2()
        operation = self.delivered()
        self.local('return 3\n')
        self.post_v2('/operation/result', id=operation['id'], outcome='applied')
        self.tick(35)
        self.assertTrue(self.post_v2('/heartbeat')['snapshot_required'])
        self.snapshot_v2([self.record('return "collaborator"\n')], token='latest')
        self.tick(3)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.base(), 'return 2\n')
        self.assertEqual(self.sync_state()['state'], 'blocked')

    def test_unchanged_autosync_scan_does_not_rewrite_state(self):
        self.snapshot()
        self.local('return 2\n')
        self.workspace.checkout('main', self.path)
        self.tick()
        changes = []
        connect = self.workspace.connect

        @contextmanager
        def traced_connect():
            with connect() as connection:
                before = connection.total_changes
                yield connection
                changes.append(connection.total_changes - before)

        self.workspace.connect = traced_connect
        self.tick(1)
        self.assertEqual(sum(changes), 0)

    def test_legacy_schema_migrates_without_losing_uncertain_operation(self):
        self.workspace.database.unlink()
        with closing(sqlite3.connect(self.workspace.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE sessions(place TEXT PRIMARY KEY,session TEXT,heartbeat REAL,complete INTEGER,
                    mode TEXT,expected INTEGER,last_snapshot REAL);
                CREATE TABLE operations(id TEXT PRIMARY KEY,place TEXT,session TEXT,path TEXT,instance_id TEXT,
                    baseline TEXT,source TEXT,state TEXT,created REAL,result TEXT DEFAULT '{}');
            ''')
            connection.execute('INSERT INTO sessions VALUES(?,?,?,1,?,1,?)', ('main', 'a', time.time(), 'Edit', time.time()))
            connection.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)', (
                'legacy', 'main', 'a', self.path, 'identity-1', 'return 1\n', 'return 2\n',
                'delivered', time.time() - 40, '{}'))
        self.restart()
        self.tick()
        self.assertEqual(self.workspace.operation('legacy')['state'], 'uncertain')
        self.assertEqual(self.session_row()['protocol_version'], 1)
        self.assertEqual(self.session_row()['epoch'], 0)


if __name__ == '__main__':
    unittest.main()

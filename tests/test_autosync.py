import time
import unittest

import test_workflow as fixtures
from studioworkflow.bridge import Workspace, read_text


class AutoSyncTest(unittest.TestCase):
    record = fixtures.WorkflowTest.record
    post = fixtures.WorkflowTest.post
    snapshot = fixtures.WorkflowTest.snapshot
    local = fixtures.WorkflowTest.local

    def setUp(self):
        fixtures.WorkflowTest.setUp(self)
        self.workspace.config['auto_sync'] = True
        self.workspace.config['sync_debounce_seconds'] = 1.5
        self.now = time.time()

    def tick(self, seconds=0):
        self.now += seconds
        self.workspace.auto_sync(self.now)

    def jobs(self):
        with self.workspace.connect() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM operations ORDER BY created')]

    def sync_state(self):
        with self.workspace.connect() as connection:
            row = connection.execute('SELECT * FROM autosync WHERE place=? AND path=?', ('main', self.path)).fetchone()
            return dict(row) if row else None

    def restart(self):
        self.workspace = Workspace(self.root)
        self.workspace.config['auto_sync'] = True

    def test_save_coalesces_and_auto_queues_without_checkout(self):
        self.snapshot()
        self.local('return 2\n')
        self.tick()
        self.tick(1)
        self.assertEqual(self.jobs(), [])
        self.local('return 3\n')
        self.tick()
        self.tick(1)
        self.assertEqual(self.jobs(), [])
        self.tick(1)
        self.assertEqual([job['source'] for job in self.jobs()], ['return 3\n'])

    def test_studio_changes_never_echo_back(self):
        self.snapshot()
        self.post('/changes', scripts=[self.record('return 3\n')])
        self.tick()
        self.tick(3)
        self.assertEqual(self.jobs(), [])
        self.assertEqual(read_text(self.workspace.work_file('main', self.path)), 'return 3\n')

    def test_newer_save_during_delivery_chains_after_verified_echo(self):
        self.snapshot()
        self.local('return 2\n')
        self.tick()
        self.tick(2)
        first = self.post('/heartbeat')['operations'][0]
        self.local('return 3\n')
        self.tick()
        self.tick(2)
        self.assertEqual(len(self.jobs()), 1)
        self.post('/changes', scripts=[self.record('return 2\n')])
        self.post('/operation/result', id=first['id'], outcome='applied')
        self.tick(2)
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(self.jobs()[1]['baseline'], 'return 2\n')
        self.assertEqual(self.jobs()[1]['source'], 'return 3\n')
        self.assertEqual(read_text(self.workspace.work_file('main', self.path)), 'return 3\n')

    def test_rejected_delivery_blocks_new_hashes_and_survives_restart(self):
        self.snapshot()
        self.local('return 2\n')
        self.tick()
        self.tick(2)
        job = self.post('/heartbeat')['operations'][0]
        self.post('/operation/result', id=job['id'], outcome='rejected')
        self.restart()
        self.local('return 3\n')
        self.tick(2)
        self.tick(2)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.sync_state()['state'], 'blocked')
        self.assertTrue(self.post('/heartbeat')['sync_issues'])
        self.workspace.resume('main', self.path)
        self.tick()
        self.tick(2)
        self.assertEqual(len(self.jobs()), 2)

    def test_never_delivered_reconnection_requeues_against_fresh_base(self):
        self.snapshot()
        self.local('return 2\n')
        self.tick()
        self.tick(2)
        self.snapshot()
        self.restart()
        self.tick(5)
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(self.jobs()[0]['state'], 'expired')
        self.assertIsNone(self.jobs()[0]['delivered_at'])
        self.assertEqual(self.jobs()[1]['baseline'], 'return 1\n')
        self.assertEqual(self.jobs()[1]['state'], 'queued')

    def test_offline_save_waits_and_remote_reconnect_conflicts(self):
        self.snapshot()
        self.post('/disconnect')
        self.local('return 2\n')
        self.tick()
        self.tick(3)
        self.assertEqual(self.jobs(), [])
        self.snapshot([self.record('return 3\n')], session='new')
        self.tick(3)
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.sync_state()['state'], 'blocked')
        self.assertEqual(read_text(self.workspace.work_file('main', self.path)), 'return 2\n')

    def test_offline_save_auto_sends_after_unchanged_reconnect(self):
        self.snapshot()
        self.post('/disconnect')
        self.local('return 2\n')
        self.tick()
        self.tick(3)
        self.snapshot(session='new')
        self.tick(2)
        self.assertEqual(len(self.jobs()), 1)

    def test_hold_preserves_read_base_and_prevents_autosend(self):
        self.snapshot()
        self.workspace.checkout('main', self.path)
        self.post('/changes', scripts=[self.record('return 3\n')])
        self.local('return 2\n')
        self.tick()
        self.tick(3)
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.sync_state()['state'], 'held')
        with self.assertRaisesRegex(ValueError, 'Studio changed'):
            self.workspace.queue_apply('main', self.path)

    def test_burst_limits_outstanding_work_per_place(self):
        self.snapshot([self.record(path=f'ReplicatedStorage/Example{number}.luau') for number in range(40)])
        for number in range(40):
            self.workspace.work_file('main', f'ReplicatedStorage/Example{number}.luau').write_text('return 2\n')
        self.tick()
        self.tick(3)
        self.assertEqual(len(self.jobs()), 1)

    def test_rebase_holds_result_for_review(self):
        self.snapshot()
        self.local('return 2\n')
        result = self.workspace.rebase('main', self.path)
        self.assertTrue(result['held'])
        self.tick()
        self.tick(3)
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.sync_state()['state'], 'held')

    def test_play_and_disabled_mode_never_autosend(self):
        self.snapshot()
        self.local('return 2\n')
        self.post('/heartbeat', mode='Play')
        self.tick()
        self.tick(3)
        self.assertEqual(self.jobs(), [])
        with self.assertRaisesRegex(ValueError, 'new complete snapshot'):
            self.post('/heartbeat')
        self.snapshot()
        self.workspace.config['auto_sync'] = False
        self.tick(3)
        self.assertEqual(self.jobs(), [])

    def test_changed_file_is_not_sent_with_old_debounce_proof(self):
        self.snapshot()
        self.local('return 2\n')
        self.tick()
        observed = self.sync_state()['observed_hash']
        self.local('return 3\n')
        result = self.workspace.queue_apply('main', self.path, automatic=True, expected_hash=observed)
        self.assertEqual(result['state'], 'changed')
        self.assertEqual(self.jobs(), [])

    def test_delivery_uncertainty_blocks_and_cannot_resume(self):
        self.snapshot()
        self.local('return 2\n')
        self.tick()
        self.tick(2)
        self.post('/heartbeat')
        self.restart()
        self.tick(35)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.sync_state()['state'], 'blocked')
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            self.workspace.resume('main', self.path)

    def test_invalid_ambiguous_and_delete_are_blocked(self):
        for kind in ('ambiguous', 'delete', 'encoding'):
            with self.subTest(kind=kind):
                self.workspace.resume('main', self.path) if self.sync_state() else None
                record = self.record()
                if kind == 'ambiguous':
                    record['metadata']['ambiguous'] = True
                self.snapshot([record])
                self.local('return 2\n')
                if kind == 'delete':
                    self.workspace.work_file('main', self.path).unlink()
                elif kind == 'encoding':
                    self.workspace.work_file('main', self.path).write_bytes(b'\xff')
                self.tick()
                self.tick(2)
                self.assertEqual(self.jobs(), [])
                self.assertEqual(self.sync_state()['state'], 'blocked')
                self.workspace.work_file('main', self.path).write_text('return 1\n')


if __name__ == '__main__':
    unittest.main()

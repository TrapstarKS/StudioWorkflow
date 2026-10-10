import json
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import test_autosync
from studioworkflow.bridge import MAX_BODY, make_server


class OperationChunksTest(unittest.TestCase):
    setUp = test_autosync.AutoSyncTest.setUp
    record = test_autosync.AutoSyncTest.record
    post = test_autosync.AutoSyncTest.post
    snapshot = test_autosync.AutoSyncTest.snapshot
    local = test_autosync.AutoSyncTest.local
    tick = test_autosync.AutoSyncTest.tick

    def prepare(self):
        baseline = 'local value = "🎲 café \\"\r\n' * 30000
        source = baseline.replace('café', 'golem', 1)
        self.snapshot([self.record(baseline)])
        self.local(source)
        self.tick()
        self.tick(2)
        return baseline, source

    @patch('studioworkflow.bridge.time.time', return_value=1000.0)
    def test_autosave_chunk_transfer_http_and_confirmation(self, clock):
        self.now = clock.return_value
        baseline, source = self.prepare()
        server = make_server(self.workspace)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}'
        client = build_opener(ProxyHandler({}))

        def request(route, authenticated=True, **payload):
            body = json.dumps(dict(place_id=1, session='a', mode='Edit', **payload)).encode()
            headers = {'X-Studio-Workflow-Key': self.workspace.key} if authenticated else {}
            try:
                with client.open(Request(url + route, data=body, headers=headers), timeout=3) as response:
                    raw = response.read()
                    self.assertLess(len(raw), MAX_BODY)
                    return json.loads(raw)
            except HTTPError as error:
                error.add_note(error.read().decode("utf-8"))
                raise

        operation = request('/heartbeat', operation_chunks=True)['operations'][0]
        self.assertNotIn('baseline', operation)
        self.assertNotIn('source', operation)
        chunks = []
        for position in range(1, operation['chunks'] + 1):
            chunk = request('/operation/chunk', id=operation['id'], position=position)
            self.assertEqual((chunk['id'], chunk['position'], chunk['total']),
                             (operation['id'], position, operation['chunks']))
            chunks.append(chunk['text'])
        self.assertEqual(json.loads(''.join(chunks)), {'baseline': baseline, 'source': source})
        self.assertEqual(request('/operation/chunk', id=operation['id'], position=1)['text'], chunks[0])
        self.assertEqual(request('/heartbeat', operation_chunks=True)['operations'], [])
        with self.assertRaises(HTTPError) as error:
            request('/operation/chunk', authenticated=False, id=operation['id'], position=1)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        self.post('/changes', scripts=[self.record(source)])
        request('/operation/result', id=operation['id'], outcome='applied')
        self.assertEqual(self.workspace.operation(operation['id'])['state'], 'applied')
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.post('/operation/chunk', id=operation['id'], position=1)

    def test_old_plugin_waits_without_receiving_or_expiring_large_job(self):
        self.prepare()
        with self.workspace.connect() as connection:
            connection.execute('UPDATE operations SET created=created-31')
        result = self.post('/heartbeat')
        self.assertEqual(result['operations'], [])
        self.assertIn('Reopen Studio', result['sync_issues'][0])
        with self.workspace.connect() as connection:
            self.assertEqual(connection.execute('SELECT state FROM operations').fetchone()['state'], 'queued')

    def test_transfer_rejects_invalid_scope_position_play_and_reconnect(self):
        baseline, _ = self.prepare()
        operation = self.post('/heartbeat', operation_chunks=True)['operations'][0]
        for position in (0, -1, operation['chunks'] + 1, 1.5, True, '1'):
            with self.subTest(position=position), self.assertRaisesRegex(ValueError, 'position'):
                self.post('/operation/chunk', id=operation['id'], position=position)
        with self.assertRaises(ValueError):
            self.post('/operation/chunk', session='other', id=operation['id'], position=1)
        self.snapshot(place=2, session='hub')
        with self.assertRaises(ValueError):
            self.post('/operation/chunk', place=2, session='hub', id=operation['id'], position=1)
        with self.assertRaisesRegex(ValueError, 'Edit mode'):
            self.post('/operation/chunk', mode='Play', id=operation['id'], position=1)
        self.post('/snapshot/start', count=1)
        with self.assertRaisesRegex(ValueError, 'complete snapshot'):
            self.post('/operation/chunk', id=operation['id'], position=1)
        self.post('/snapshot/batch', scripts=[self.record(baseline)])
        self.post('/snapshot/finish')
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.post('/operation/chunk', id=operation['id'], position=1)
        self.assertEqual(self.post('/heartbeat', operation_chunks=True)['operations'], [])

    @unittest.skipUnless(shutil.which('lune'), 'Lune required for observer execution')
    def test_observer_assembles_before_write_and_preserves_race_guards(self):
        baseline, source = self.prepare()
        operation = self.post('/heartbeat', operation_chunks=True)['operations'][0]
        chunks = [self.post('/operation/chunk', id=operation['id'], position=position)
                  for position in range(1, operation['chunks'] + 1)]
        fixture = self.root / 'chunks.json'
        fixture.write_text(json.dumps(dict(operation=operation, chunks=chunks, baseline=baseline, source=source)))
        harness = Path(__file__).with_name('operation_chunks.luau')
        observer = Path(__file__).resolve().parents[1] / 'studioworkflow/observer.luau'
        subprocess.run(['lune', 'run', str(harness), str(observer), str(fixture)], check=True, capture_output=True)

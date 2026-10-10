import concurrent.futures
import json
from pathlib import Path
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_workflow
from studioworkflow import websocket_wakeup
from studioworkflow.bridge import make_server

class HttpWakeupFallbackTest(unittest.TestCase):
    record = test_workflow.WorkflowTest.record
    post = test_workflow.WorkflowTest.post
    snapshot = test_workflow.WorkflowTest.snapshot
    local = test_workflow.WorkflowTest.local

    def setUp(self):
        test_workflow.WorkflowTest.setUp(self)
        self.snapshot()
        self.server = make_server(self.workspace)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.address = self.server.server_address
        self.url = 'http://%s:%s' % self.address
        self.client = build_opener(ProxyHandler({}))

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def http(self, route, payload=None, key=None):
        headers = {"X-Studio-Workflow-Key": self.workspace.key if key is None else key}
        body = json.dumps(payload).encode() if payload is not None else None
        with self.client.open(Request(self.url + route, data=body, headers=headers), timeout=3) as response:
            return json.load(response)

    def queue(self):
        self.local('return 2\n')
        return self.workspace.queue_apply('main', self.path)['id']

    def test_wakeup_authentication(self):
        for route in ('/wakeup?place_id=1&session=a', '/health'):
            with self.assertRaises(HTTPError) as raised:
                self.http(route, key='wrong')
            self.assertEqual(raised.exception.code, 401)
            raised.exception.close()

    def test_http_delivery_has_no_websocket_dependency(self):
        self.assertFalse(self.http('/health')['wakeup']['available'])
        with self.assertRaises(HTTPError) as raised:
            self.http('/wakeup?place_id=1&session=a')
        self.assertEqual(raised.exception.code, 503)
        raised.exception.close()
        operation = self.queue()
        result = self.http('/heartbeat', {'place_id': 1, 'session': 'a', 'mode': 'Edit'})
        self.assertEqual([job['id'] for job in result['operations']], [operation])

    def test_legacy_configuration_cannot_enable_removed_transport(self):
        for setting in (False, True):
            self.workspace.config['websocket_wakeup'] = setting
            self.assertFalse(websocket_wakeup.Wakeups(self.workspace).enabled)

    def test_unavailable_wakeup_and_unknown_place_leave_queue_untouched(self):
        operation = self.queue()
        for route in ('/wakeup?place_id=1&session=a', '/wakeup?place_id=999&session=a'):
            with self.assertRaises(HTTPError) as raised:
                self.http(route)
            self.assertEqual(raised.exception.code, 503)
            raised.exception.close()
        self.assertEqual(self.workspace.operation(operation)['state'], 'queued')

    def test_health_reads_do_not_claim_pending_operations(self):
        operation = self.queue()
        health = self.http('/health')
        self.assertEqual(health['wakeup'], {'available': False, 'connections': 0})
        self.assertEqual(self.workspace.operation(operation)['state'], 'queued')
        self.http('/health')
        self.assertEqual(self.workspace.operation(operation)['state'], 'queued')
        self.assertTrue(self.workspace.status()['main']['state'] == 'ready')

    def test_concurrent_http_claim_is_unique_and_uncertain_delivery_not_retried(self):
        operation = self.queue()
        payload = {'place_id': 1, 'session': 'a', 'mode': 'Edit'}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            responses = list(executor.map(lambda _: self.http('/heartbeat', payload), range(6)))
        delivered = [job['id'] for response in responses for job in response['operations']]
        self.assertEqual(delivered, [operation])
        self.assertEqual(self.http('/heartbeat', payload)['operations'], [])
        self.assertEqual(self.workspace.operation(operation)['state'], 'delivered')
        with self.assertRaisesRegex(ValueError, 'pending'):
            self.workspace.queue_apply('main', self.path)

    def test_retired_wakeup_route_cannot_acknowledge_operation(self):
        operation = self.queue()
        with self.assertRaises(HTTPError) as raised:
            self.http('/wakeup', {'place_id': 1, 'session': 'a', 'id': operation, 'outcome': 'applied'})
        self.assertEqual(raised.exception.code, 409)
        raised.exception.close()
        self.assertEqual(self.workspace.operation(operation)['state'], 'queued')


if __name__ == '__main__':
    unittest.main()

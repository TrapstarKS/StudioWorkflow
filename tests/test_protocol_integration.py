import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import test_workflow
from studioworkflow.bridge import Workspace, atomic_write, make_server, read_text


class ProtocolIntegrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        config = self.root / "studio-workflow.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({
            "version": 1, "installation_id": "0123456789abcdef", "minimum_observer_protocol": 1, "port": 8647, "heartbeat_timeout_seconds": 20, "auto_sync": True,
            "sync_debounce_seconds": 0, "minimum_observer_protocol": 2,
            "places": {"main": {"place_id": 1}, "hub": {"place_id": 2}},
        }))
        self.workspace = Workspace(self.root)
        self.workspace.config["port"] = 0
        self.server = make_server(self.workspace)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.client = build_opener(ProxyHandler({}))
        self.path = "ReplicatedStorage/Example.luau"
        self.request_sequence = 0

    def post(self, route, *, place=1, epoch=1, protocol=2, opener=None, **fields):
        payload = {"place_id": place, "session": "observer-" + str(place), "mode": "Edit",
                   "epoch": epoch, "protocol_version": protocol, **fields}
        if protocol >= 3 and "request_id" not in payload:
            self.request_sequence += 1
            payload["request_id"] = "request-" + str(self.request_sequence)
        request = Request(self.url + route, json.dumps(payload).encode(), {
            "Content-Type": "application/json", "X-Studio-Workflow-Key": self.workspace.key,
        })
        try:
            with (opener or self.client).open(request, timeout=5) as response:
                return json.load(response)
        except HTTPError as error:
            error.add_note(error.read().decode("utf-8"))
            raise

    def record(self, source="return 1\n"):
        return {"path": self.path, "source": source, "instance_path": "ReplicatedStorage.Example",
                "metadata": {"instance_id": "script-identity"}, "sync_status": "Bridge"}

    def snapshot(self, *, place=1, epoch=1, protocol=2, source="return 1\n"):
        identity = f"snapshot-{place}-{epoch}-{protocol}"
        self.post("/snapshot/start", place=place, epoch=epoch, protocol=protocol,
                  count=1, snapshot_id=identity)
        self.post("/snapshot/batch", place=place, epoch=epoch, protocol=protocol,
                  scripts=[self.record(source)], snapshot_id=identity)
        return self.post("/snapshot/finish", place=place, epoch=epoch, protocol=protocol,
                         snapshot_id=identity)

    def test_old_plugin_mirrors_but_cannot_receive_writes_until_new_inventory(self):
        self.snapshot(protocol=1)
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), "return 1\n")
        atomic_write(self.workspace.work_file("main", self.path), "return 2\n")
        with self.assertRaises(ValueError):
            self.workspace.queue_apply("main", self.path)
        self.assertEqual(self.post("/heartbeat", protocol=1)["operations"], [])
        self.snapshot(protocol=2, epoch=2)
        operation = self.workspace.queue_apply("main", self.path)
        jobs = self.post("/heartbeat", epoch=2)["operations"]
        self.assertEqual([job["id"] for job in jobs], [operation["id"]])

    def test_play_then_edit_rejects_old_packets_and_waits_for_fresh_inventory(self):
        self.snapshot()
        self.post("/heartbeat", epoch=2, mode="Play")
        with self.assertRaises(HTTPError) as error:
            self.post("/changes", epoch=1, scripts=[self.record("return 'stale'\n")])
        self.assertEqual(error.exception.code, 409)
        error.exception.close()
        edit = self.post("/heartbeat", epoch=3)
        self.assertFalse(edit["complete"])
        self.assertEqual(edit["operations"], [])
        atomic_write(self.workspace.work_file("main", self.path), "return 'local'\n")
        with self.assertRaises(ValueError):
            self.workspace.queue_apply("main", self.path)
        self.snapshot(epoch=3, source="return 'studio'\n")
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), "return 'local'\n")
        with self.assertRaises(ValueError):
            self.workspace.queue_apply("main", self.path)

    def test_lost_result_recovers_from_exact_echo_and_does_not_block_other_place(self):
        self.snapshot()
        self.snapshot(place=2)
        atomic_write(self.workspace.work_file("main", self.path), "return 2\n")
        operation = self.workspace.queue_apply("main", self.path)
        job = self.post("/heartbeat")["operations"][0]
        self.assertEqual(job["id"], operation["id"])
        atomic_write(self.workspace.work_file("main", self.path), "return 3\n")
        self.post("/changes", scripts=[self.record(job["source"])])
        self.assertEqual(self.workspace.operation(operation["id"])["state"], "applied")
        next_operation = self.workspace.queue_apply("main", self.path)
        next_job = self.post("/heartbeat")["operations"][0]
        self.assertEqual(next_job["id"], next_operation["id"])
        self.assertEqual(next_job["baseline"], "return 2\n")
        self.assertEqual(next_job["source"], "return 3\n")
        self.assertEqual(self.post("/heartbeat", place=2)["operations"], [])
        self.assertEqual(read_text(self.workspace.work_file("hub", self.path)), "return 1\n")

    def test_persistent_pause_blocks_delivery_and_chunks_without_blocking_mirror(self):
        self.snapshot()
        self.workspace.set_outbound_paused(True, reason="maintenance")
        self.post("/changes", scripts=[self.record("return 'Studio remains authoritative'\n")])
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), "return 'Studio remains authoritative'\n")
        atomic_write(self.workspace.work_file("main", self.path), "return 'local edit'\n")
        with self.assertRaises(ValueError):
            self.workspace.queue_apply("main", self.path)
        heartbeat = self.post("/heartbeat")
        self.assertTrue(heartbeat["outbound_paused"])
        self.assertEqual(heartbeat["operations"], [])
        with self.assertRaises(HTTPError) as error:
            self.post("/operation/chunk", id="unknown", position=1)
        error.exception.close()

    def test_busy_server_requests_retry_without_acknowledging_unprocessed_change(self):
        self.snapshot()
        locked = threading.Event()
        release = threading.Event()

        def hold_snapshot_lock():
            with self.workspace.lock:
                locked.set()
                release.wait(5)

        holder = threading.Thread(target=hold_snapshot_lock, daemon=True)
        holder.start()
        self.assertTrue(locked.wait(2))
        try:
            response = self.post("/changes", scripts=[self.record("return 'pending request'\n")])
            self.assertTrue(response["retry"])
            self.assertEqual(read_text(self.workspace.work_file("main", self.path)), "return 1\n")
        finally:
            release.set()
            holder.join(2)
        self.post("/changes", scripts=[self.record("return 'pending request'\n")])
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), "return 'pending request'\n")

    def test_protocol_three_replays_a_lost_heartbeat_response(self):
        self.snapshot(protocol=3)
        atomic_write(self.workspace.work_file("main", self.path), "return 2\n")
        operation = self.workspace.queue_apply("main", self.path)
        first = self.post("/heartbeat", protocol=3, request_id="lost-heartbeat")
        second = self.post("/heartbeat", protocol=3, request_id="lost-heartbeat")
        self.assertEqual(first, second)
        self.assertEqual([job["id"] for job in second["operations"]], [operation["id"]])
        self.assertEqual(self.post("/heartbeat", protocol=3, request_id="next-heartbeat")["operations"], [])
        with self.assertRaises(HTTPError) as error:
            self.post("/heartbeat", protocol=3, request_id="lost-heartbeat", snapshot_incomplete=True)
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

    def test_protocol_three_requires_request_identity(self):
        with self.assertRaises(HTTPError) as error:
            self.post("/snapshot/start", protocol=3, request_id="", count=0, snapshot_id="missing-request")
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

    @patch("studioworkflow.bridge.time.time", return_value=1000.0)
    def test_slow_snapshot_finish_refreshes_liveness_when_it_commits(self, clock):
        self.workspace.config["heartbeat_timeout_seconds"] = 0.1
        original = self.workspace.apply_record

        def slow_apply(*arguments, **keywords):
            clock.return_value += 0.15
            return original(*arguments, **keywords)

        with patch.object(self.workspace, "apply_record", side_effect=slow_apply):
            self.snapshot(protocol=3)

        heartbeat = self.post("/heartbeat", protocol=3)
        self.assertTrue(heartbeat["complete"])
        self.assertFalse(heartbeat["snapshot_required"])

    def test_main_and_hub_snapshot_finishes_queue_without_losing_either_place(self):
        snapshots = {1: "main-concurrent-snapshot", 2: "hub-concurrent-snapshot"}
        sources = {1: "return 'main'\n", 2: "return 'hub'\n"}
        for place in (1, 2):
            self.post("/snapshot/start", place=place, protocol=3, count=1,
                      snapshot_id=snapshots[place])
            self.post("/snapshot/batch", place=place, protocol=3,
                      scripts=[self.record(sources[place])], snapshot_id=snapshots[place])

        main_started = threading.Event()
        release_main = threading.Event()
        main_results = []
        main_errors = []
        original = self.workspace.apply_record

        def block_main(connection, place, record, **keywords):
            if place == "main" and not main_started.is_set():
                main_started.set()
                release_main.wait(2)
            return original(connection, place, record, **keywords)

        def finish_main():
            try:
                main_results.append(self.post(
                    "/snapshot/finish", place=1, protocol=3, request_id="main-finish",
                    snapshot_id=snapshots[1], opener=build_opener(ProxyHandler({})),
                ))
            except BaseException as error:
                main_errors.append(error)

        with patch.object(self.workspace, "apply_record", side_effect=block_main):
            main_thread = threading.Thread(target=finish_main, daemon=True)
            main_thread.start()
            self.assertTrue(main_started.wait(1))
            try:
                busy = self.post(
                    "/snapshot/finish", place=2, protocol=3, request_id="hub-finish",
                    snapshot_id=snapshots[2],
                )
                self.assertTrue(busy["retry"])
            finally:
                release_main.set()
                main_thread.join(2)

        self.assertFalse(main_thread.is_alive())
        self.assertEqual(main_errors, [])
        self.assertTrue(main_results[0]["ok"])
        hub_result = self.post(
            "/snapshot/finish", place=2, protocol=3, request_id="hub-finish",
            snapshot_id=snapshots[2],
        )
        hub_replay = self.post(
            "/snapshot/finish", place=2, protocol=3, request_id="hub-finish",
            snapshot_id=snapshots[2],
        )
        self.assertEqual(hub_result, hub_replay)
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), sources[1])
        self.assertEqual(read_text(self.workspace.work_file("hub", self.path)), sources[2])
        self.assertEqual(self.workspace.status()["main"]["state"], "ready")
        self.assertEqual(self.workspace.status()["hub"]["state"], "ready")
        with self.workspace.connect() as connection:
            snapshots_by_place = dict(connection.execute(
                "SELECT place,count(*) FROM events WHERE kind='snapshot' GROUP BY place"
            ).fetchall())
        self.assertEqual(snapshots_by_place, {"hub": 1, "main": 1})

    @patch("studioworkflow.bridge.time.time", return_value=1000.0)
    def test_busy_retries_keep_the_other_place_live_during_snapshot_commit(self, clock):
        self.workspace.config["heartbeat_timeout_seconds"] = 0.15
        self.snapshot(place=1, protocol=3)
        hub_snapshot = "slow-hub-snapshot"
        self.post("/snapshot/start", place=2, protocol=3, count=1, snapshot_id=hub_snapshot)
        self.post("/snapshot/batch", place=2, protocol=3,
                  scripts=[self.record("return 'hub'\n")], snapshot_id=hub_snapshot)

        hub_started = threading.Event()
        release_hub = threading.Event()
        hub_results = []
        hub_errors = []
        original = self.workspace.apply_record

        def block_hub(connection, place, record, **keywords):
            if place == "hub" and not hub_started.is_set():
                hub_started.set()
                release_hub.wait(2)
            return original(connection, place, record, **keywords)

        def finish_hub():
            try:
                hub_results.append(self.post(
                    "/snapshot/finish", place=2, protocol=3, request_id="slow-hub-finish",
                    snapshot_id=hub_snapshot, opener=build_opener(ProxyHandler({})),
                ))
            except BaseException as error:
                hub_errors.append(error)

        with patch.object(self.workspace, "apply_record", side_effect=block_hub):
            hub_thread = threading.Thread(target=finish_hub, daemon=True)
            hub_thread.start()
            self.assertTrue(hub_started.wait(1))
            try:
                for heartbeat_index in range(4):
                    clock.return_value += 0.1
                    keepalive = self.post(
                        "/heartbeat", place=1, protocol=3,
                        request_id="main-waiting-heartbeat-" + str(heartbeat_index),
                    )
                    self.assertTrue(keepalive["complete"])
                    self.assertFalse(keepalive["snapshot_required"])
                    self.assertEqual(keepalive["operations"], [])
            finally:
                release_hub.set()
                hub_thread.join(2)

        self.assertFalse(hub_thread.is_alive())
        self.assertEqual(hub_errors, [])
        self.assertTrue(hub_results[0]["ok"])
        heartbeat = self.post(
            "/heartbeat", place=1, protocol=3, request_id="main-after-hub-finish",
        )
        self.assertTrue(heartbeat["complete"])
        self.assertFalse(heartbeat["snapshot_required"])
        self.assertEqual(self.workspace.status()["main"]["state"], "ready")
        self.assertEqual(self.workspace.status()["hub"]["state"], "ready")

    @patch("studioworkflow.bridge.time.time", return_value=1000.0)
    def test_busy_heartbeat_cannot_revive_a_session_that_was_already_expired(self, clock):
        self.workspace.config["heartbeat_timeout_seconds"] = 0.05
        self.snapshot(protocol=3)
        with self.workspace.connect() as connection:
            connection.execute("UPDATE sessions SET heartbeat=heartbeat-1 WHERE place='main'")

        locked = threading.Event()
        release = threading.Event()

        def hold_workspace():
            with self.workspace.lock:
                locked.set()
                release.wait(2)

        holder = threading.Thread(target=hold_workspace, daemon=True)
        holder.start()
        self.assertTrue(locked.wait(1))
        try:
            busy = self.post(
                "/heartbeat", protocol=3, request_id="expired-busy-heartbeat",
            )
            self.assertTrue(busy["retry"])
        finally:
            release.set()
            holder.join(2)

        heartbeat = self.post(
            "/heartbeat", protocol=3, request_id="expired-busy-heartbeat",
        )
        self.assertFalse(heartbeat["complete"])
        self.assertTrue(heartbeat["snapshot_required"])

    def test_disconnected_client_does_not_raise_while_responding(self):
        handler = object.__new__(self.server.RequestHandlerClass)
        handler.send_response = lambda code: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None

        class ClosedStream:
            def write(self, data):
                raise BrokenPipeError("client closed")

        handler.wfile = ClosedStream()
        self.assertFalse(handler.respond(200, {"ok": True}))

    def test_unknown_ack_after_database_recovery_is_terminal_and_requests_inventory(self):
        result = self.post("/operation/result", id="previous-database-operation", outcome="applied")
        self.assertTrue(result["terminal"])
        self.assertTrue(result["snapshot_required"])
        self.snapshot()
        self.assertEqual(self.post("/heartbeat")["operations"], [])

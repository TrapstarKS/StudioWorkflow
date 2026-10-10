import json
import time
import unittest
from unittest.mock import patch

import test_workflow as fixtures
from studioworkflow.bridge import digest, safe_relative


class ReleaseRegressions(unittest.TestCase):
    record = fixtures.WorkflowTest.record
    post = fixtures.WorkflowTest.post
    snapshot = fixtures.WorkflowTest.snapshot
    local = fixtures.WorkflowTest.local

    def setUp(self):
        fixtures.WorkflowTest.setUp(self)
        self.snapshot()

    def test_search_finds_held_and_offline_local_edits(self):
        self.workspace.checkout("main", self.path)
        self.local("local UnsentTerm = true\nreturn UnsentTerm")
        result = self.workspace.query("UnsentTerm", "main", 5)
        self.assertEqual(result["search_tree"], "Files")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(self.workspace.query("UnsentTerm", "main", 5, observed=True)["results"], [])
        self.post("/disconnect")
        result = self.workspace.query("UnsentTerm", "main", 5)
        self.assertEqual(result["status"]["main"]["state"], "offline")
        self.assertEqual(len(result["results"]), 1)

    def test_search_never_returns_an_unbounded_minified_excerpt(self):
        self.local("return " + "x" * 100000)
        result = self.workspace.query("return", "main", 1)
        self.assertLessEqual(len(result["results"][0]["excerpt"]), 500)
        self.post("/changes", scripts=[self.record("return " + "x" * 100000)])
        result = self.workspace.query("return", "main", 1, observed=True)
        self.assertLessEqual(len(result["results"][0]["excerpt"]), 500)

    def test_windows_aliased_components_rejected_on_every_platform(self):
        for path in ("CON.luau", "aux/Script.luau", "Folder /Script.luau", "Folder./Script.luau",
                     "COM1/Script.luau", "file:stream.luau", "a\x01.luau", "A?/script.luau"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_relative(path)
        self.assertEqual(safe_relative("%43ON/Example%20.luau"), "%43ON/Example%20.luau")

    def test_old_plugin_cannot_dequeue_with_default_release_protocol(self):
        self.workspace.config["minimum_observer_protocol"] = 3
        self.local("return 2\n")
        with self.assertRaisesRegex(ValueError, "Restart Studio"):
            self.workspace.queue_apply("main", self.path)
        self.assertEqual(self.post("/heartbeat")["operations"], [])

    def test_record_limit_accepts_boundary_and_rejects_next_byte(self):
        record = self.record("return 1")
        size = len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
        with patch("studioworkflow.bridge.MAX_RECORD_BYTES", size):
            self.workspace.normalize(record)
            with self.assertRaisesRegex(ValueError, "bridge limit"):
                self.workspace.normalize({**record, "source": "return 11"})

    def test_oversized_local_edit_is_not_queued_and_keeps_checkout(self):
        self.workspace.checkout("main", self.path)
        self.local("return 22222222222222222222")
        with patch("studioworkflow.bridge.MAX_RECORD_BYTES", 10), self.assertRaisesRegex(ValueError, "bridge limit"):
            self.workspace.queue_apply("main", self.path)
        with self.workspace.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 1)

    def test_outbound_chunk_count_boundary_is_checked_before_queueing(self):
        self.local("return 2")
        size = len(json.dumps({"baseline": "return 1\n", "source": "return 2"}, ensure_ascii=False))
        with patch("studioworkflow.bridge.OPERATION_CHUNK_CHARACTERS", size), patch("studioworkflow.bridge.MAX_OPERATION_CHUNKS", 1):
            operation = self.workspace.queue_apply("main", self.path)
            self.assertEqual(operation["state"], "queued")
        with self.workspace.connect() as connection:
            connection.execute("DELETE FROM operations")
        self.local("return 22")
        with patch("studioworkflow.bridge.OPERATION_CHUNK_CHARACTERS", size), patch("studioworkflow.bridge.MAX_OPERATION_CHUNKS", 1):
            with self.assertRaisesRegex(ValueError, "1000-chunk"):
                self.workspace.queue_apply("main", self.path)

    def test_record_chunk_types_and_boundary(self):
        for position, total, text in ((True, 1, "{}"), (1, "1", "{}"), (1, 101, "{}"),
                                      (1, 1, {}), (1, 1, "x" * 200001)):
            with self.subTest(position=position, total=total), self.assertRaises(ValueError):
                self.post("/record/chunk", endpoint="/changes", transfer="bad", position=position, total=total, text=text)
        self.post("/record/chunk", endpoint="/changes", transfer="boundary", position=1, total=100, text="x" * 200000)

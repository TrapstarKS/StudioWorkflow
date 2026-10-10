import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studioworkflow.bridge import Workspace, atomic_write, digest, inside, make_server, read_text, reconcile, safe_relative


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        config = self.root / "studio-workflow.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"version": 1, "installation_id": "0123456789abcdef", "minimum_observer_protocol": 1, "port": 8647, "heartbeat_timeout_seconds": 20,
                                      "places": {"main": {"place_id": 1}, "hub": {"place_id": 2}}}))
        self.workspace = Workspace(self.root)
        self.workspace.config["port"] = 0
        self.path = "ReplicatedStorage/Example.luau"

    def record(self, source="return 1\n", path=None):
        return {"path": path or self.path, "source": source, "instance_path": "ReplicatedStorage.Example",
                "metadata": {"instance_id": "identity-1"}, "sync_status": "Bridge"}

    def post(self, route, place=1, session="a", **kwargs):
        return self.workspace.receive(route, {"place_id": place, "session": session, "mode": "Edit", **kwargs})

    def snapshot(self, records=None, place=1, session="a"):
        records = records if records is not None else [self.record()]
        self.post("/snapshot/start", place, session, count=len(records))
        self.post("/snapshot/batch", place, session, scripts=records)
        return self.post("/snapshot/finish", place, session)

    def local(self, source):
        atomic_write(self.workspace.work_file("main", self.path), source)

    def test_exact_source_unicode_newlines_and_compound_assignment(self):
        content = 'local café = "🎲"\r\ncafé += 2\r\nreturn café'
        self.snapshot([self.record(content)])
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), content)
        self.assertEqual(read_text(self.root / "studio/main/Source" / self.path), content)

    def test_snapshot_is_not_ready_until_complete(self):
        self.post("/snapshot/start", count=2)
        self.post("/snapshot/batch", scripts=[self.record()])
        with self.assertRaises(ValueError):
            self.post("/snapshot/finish")
        self.assertNotEqual(self.workspace.status()["main"]["state"], "ready")
        with self.assertRaises(ValueError):
            self.workspace.queue_apply("main", self.path)

    def test_missing_generated_source_is_restored_on_snapshot(self):
        self.snapshot()
        source = self.root / "studio/main/Source" / self.path
        source.chmod(0o600)
        source.unlink()
        self.snapshot()
        self.assertEqual(source.read_text(), "return 1\n")

    def test_unsaved_local_work_survives_remote_change(self):
        self.snapshot()
        self.local("return 'local'\n")
        self.post("/changes", scripts=[self.record("return 'collaborator'\n")])
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), "return 'local'\n")
        with self.assertRaisesRegex(ValueError, "Studio changed"):
            self.workspace.queue_apply("main", self.path)
        outcome = self.workspace.rebase("main", self.path)
        self.assertEqual(outcome["action"], "conflict")
        conflict = json.loads(Path(outcome["file"]).read_text())
        self.assertEqual(conflict["studio"], "return 'collaborator'\n")

    def test_clean_local_file_follows_remote(self):
        self.snapshot()
        self.post("/changes", scripts=[self.record("return 2\n")])
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), "return 2\n")

    def test_deletion_preserves_dirty_local_file(self):
        self.snapshot()
        self.local("return 'pending'\n")
        self.post("/changes", deleted=[self.path])
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), "return 'pending'\n")
        self.assertFalse((self.root / "studio/main/Source" / self.path).exists())
        self.assertEqual(self.workspace.rebase("main", self.path)["action"], "conflict")

    def test_clean_deleted_file_is_removed(self):
        self.snapshot()
        self.post("/changes", deleted=[self.path])
        self.assertFalse(self.workspace.work_file("main", self.path).exists())

    def test_local_deletion_does_not_queue_studio_deletion(self):
        self.snapshot()
        self.workspace.work_file("main", self.path).unlink()
        with self.assertRaisesRegex(ValueError, "no implicit"):
            self.workspace.queue_apply("main", self.path)

    def test_offline_blocks_writes(self):
        self.snapshot()
        self.local("return 2\n")
        self.post("/disconnect")
        with self.assertRaisesRegex(ValueError, "offline"):
            self.workspace.queue_apply("main", self.path)

    def test_play_blocks_writes(self):
        self.snapshot()
        self.post("/heartbeat", mode="Play")
        with self.assertRaisesRegex(ValueError, "playing"):
            self.workspace.queue_apply("main", self.path)

    def test_separate_places_do_not_overwrite_each_other(self):
        self.snapshot()
        self.snapshot([self.record("return 'hub'")], place=2)
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), "return 1\n")
        self.assertEqual(read_text(self.workspace.work_file("hub", self.path)), "return 'hub'")

    def test_queue_delivery_exactly_once_and_ack(self):
        self.snapshot()
        self.local("return 2\n")
        queued = self.workspace.queue_apply("main", self.path)
        operations = self.post("/heartbeat")["operations"]
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["baseline"], "return 1\n")
        self.assertEqual(self.post("/heartbeat")["operations"], [])
        self.post("/changes", scripts=[self.record("return 2\n")])
        self.post("/operation/result", id=queued["id"], outcome="applied")
        self.assertEqual(self.workspace.operation(queued["id"])["state"], "applied")

    def test_duplicate_pending_operation_blocked(self):
        self.snapshot()
        self.local("return 2\n")
        self.workspace.queue_apply("main", self.path)
        with self.assertRaisesRegex(ValueError, "pending"):
            self.workspace.queue_apply("main", self.path)

    def test_reconnect_expires_pending_edits(self):
        self.snapshot()
        self.local("return 'pending'\n")
        queued = self.workspace.queue_apply("main", self.path)
        self.post("/disconnect")
        self.snapshot([self.record("return 'collaborator'\n")], session="b")
        self.assertEqual(self.workspace.operation(queued["id"])["state"], "expired")
        self.assertEqual(self.post("/heartbeat", session="b")["operations"], [])
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), "return 'pending'\n")

    def test_active_second_observer_rejected(self):
        self.snapshot()
        with self.assertRaisesRegex(ValueError, "already active"):
            self.snapshot(session="b")

    def test_unknown_place_rejected(self):
        with self.assertRaisesRegex(ValueError, "allowlisted"):
            self.snapshot(place=999)

    def test_case_collision_blocks_snapshot(self):
        with self.assertRaisesRegex(ValueError, "collision"):
            self.snapshot([self.record(path="A.luau"), self.record(path="a.luau")])

    def test_incremental_case_collision_is_rejected(self):
        self.snapshot([self.record(path="A.luau")])
        with self.assertRaisesRegex(ValueError, "collision"):
            self.post("/changes", scripts=[self.record("return 2\n", path="a.luau")])
        self.assertEqual(read_text(self.workspace.work_file("main", "A.luau")), "return 1\n")

    def test_ambiguous_instance_blocks_apply(self):
        record = self.record()
        record["metadata"]["ambiguous"] = True
        self.snapshot([record])
        self.local("return 2\n")
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            self.workspace.queue_apply("main", self.path)

    def test_path_traversal_and_symlink_rejected(self):
        for value in ("../outside.luau", "/tmp/x", "a/../b", "a//b", "a\\b", "a/./b"):
            with self.assertRaises(ValueError):
                safe_relative(value)
        (self.root / "escape").symlink_to(self.root.parent)
        with self.assertRaises(ValueError):
            inside(self.root, "escape/x.luau")

    def test_large_chunked_record_exactness(self):
        content = 'local value = "🎲"\n' * 60000
        record = json.dumps(self.record(content), ensure_ascii=False)
        chunks = [record[index:index + 150000] for index in range(0, len(record), 150000)]
        self.post("/snapshot/start", count=1)
        for index, chunk in enumerate(chunks, 1):
            self.post("/record/chunk", endpoint="/snapshot/batch", transfer="x", position=index, total=len(chunks), text=chunk)
        self.post("/snapshot/finish")
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), content)

    def test_thousand_changes_catch_up_without_replaying_old_operations(self):
        self.snapshot()
        self.post("/disconnect")
        records = [self.record(f"return {index}\n", f"Folder/Script{index}.luau") for index in range(1000)]
        self.snapshot(records, session="b")
        self.assertEqual(self.workspace.status()["main"]["scripts"], 1000)
        self.assertFalse(self.workspace.work_file("main", self.path).exists())

    def test_search_returns_local_paths_and_freshness(self):
        self.snapshot([self.record("local BattleManager = {}\nreturn BattleManager")])
        result = self.workspace.query("BattleManager", "main", 5)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["status"], {"main": {"state": "ready"}})
        self.assertEqual(set(result["results"][0]), {"place", "path", "excerpt", "source_file", "edit_file"})
        self.assertEqual(result["results"][0]["edit_file"], "studio/main/Files/ReplicatedStorage/Example.luau")

        full = self.workspace.query("BattleManager", "main", 5, full=True)
        self.assertEqual(full["status"]["main"]["scripts"], 1)
        self.assertIn("hash", full["results"][0])
        self.assertTrue(Path(full["results"][0]["edit_file"]).is_absolute())

    def test_compact_search_keeps_actionable_sync_health(self):
        self.snapshot([self.record("local BattleManager = {}\nreturn BattleManager")])
        self.workspace.set_outbound_paused(True, "maintenance", "main")
        with self.workspace.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO autosync VALUES(?,?,?,?,?,?,?)",
                ("main", self.path, None, time.time(), "blocked", "Studio changed", None),
            )
            connection.execute(
                "INSERT OR REPLACE INTO autosync VALUES(?,?,?,?,?,?,?)",
                ("main", "ReplicatedStorage/Pending.luau", None, time.time(), "pending", "", None),
            )
        health = self.workspace.query("BattleManager", "main", 1)["status"]["main"]
        self.assertEqual(health["state"], "ready")
        self.assertTrue(health["outbound_paused"])
        self.assertEqual(health["pause_reason"], "maintenance")
        self.assertEqual(health["sync_pending"], 1)
        self.assertEqual(health["sync_issue_count"], 1)
        self.assertEqual(health["sync_issue_paths"], [self.path])

    def test_compact_search_keeps_nonready_state_signals(self):
        self.snapshot([self.record("local BattleManager = {}\nreturn BattleManager")])
        with self.workspace.connect() as connection:
            connection.execute("UPDATE sessions SET mode='Play',complete=1 WHERE place='main'")
        play = self.workspace.query("BattleManager", "main", 1)["status"]["main"]
        self.assertEqual(play["state"], "play")
        self.assertIn("heartbeat_age_seconds", play)

        self.post("/heartbeat", mode="Play")
        refreshing = self.workspace.query("BattleManager", "main", 1)["status"]["main"]
        self.assertEqual(refreshing["state"], "refreshing")

        self.post("/disconnect")
        offline = self.workspace.query("BattleManager", "main", 1)["status"]["main"]
        self.assertEqual(offline["state"], "offline")

        self.snapshot([self.record("local BattleManager = {}\nreturn BattleManager")], session="b")
        self.workspace.config["minimum_observer_protocol"] = 2
        restart = self.workspace.query("BattleManager", "main", 1)["status"]["main"]
        self.assertEqual(restart["state"], "restart_required")
        self.assertTrue(restart["restart_required"])

    def test_read_only_workspace_uses_existing_index_without_initializing_state(self):
        self.snapshot([self.record("local BattleManager = {}\nreturn BattleManager")])
        read_only = Workspace(self.root, read_only=True)
        self.assertEqual(read_only.query("BattleManager", "main", 1)["status"], {"main": {"state": "ready"}})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "studio-workflow.json"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(json.dumps({"version": 1, "installation_id": "0123456789abcdef", "minimum_observer_protocol": 1, "port": 8647, "heartbeat_timeout_seconds": 20,
                                          "places": {"main": {"place_id": 1}}}))
            with self.assertRaisesRegex(ValueError, "index is unavailable"):
                Workspace(root, read_only=True)
            self.assertFalse((root / ".studio-workflow").exists())

    def test_negative_search_limit_cannot_dump_entire_index(self):
        self.snapshot([self.record("return 1", f"A{i}.luau") for i in range(5)])
        self.assertEqual(len(self.workspace.query("return", "main", -1)["results"]), 1)

    def test_nonoverlapping_merge(self):
        base = "a\nb\nc\nd\ne\nf\ng\nh\n"
        self.assertEqual(reconcile(base, base.replace("a\n", "A\n"), base.replace("h\n", "H\n")),
                         {"action": "merge", "content": "A\nb\nc\nd\ne\nf\ng\nH\n"})

    def test_merge_preserves_crlf(self):
        base = "a\r\nb\r\nc\r\nd\r\ne\r\nf\r\ng\r\nh\r\n"
        result = reconcile(base, base.replace("a\r", "A\r"), base.replace("h\r", "H\r"))
        self.assertEqual(result["content"], "A\r\nb\r\nc\r\nd\r\ne\r\nf\r\ng\r\nH\r\n")

    def test_checkout_protects_file_even_before_first_keystroke(self):
        self.snapshot()
        self.workspace.checkout("main", self.path)
        self.post("/changes", scripts=[self.record("return 'collaborator'\n")])
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), "return 1\n")
        self.workspace.release("main", self.path)
        self.assertEqual(read_text(self.workspace.work_file("main", self.path)), "return 'collaborator'\n")

    def test_checkpoint_isolated_git_and_recovery(self):
        self.snapshot()
        result = self.workspace.checkpoint("main")
        self.assertEqual(len(result["commit"]), 40)
        self.assertFalse((self.root / ".git").exists())
        self.assertEqual(self.workspace.checkpoint("main")["commit"], result["commit"])
        self.post("/changes", scripts=[self.record("return 2\n")])
        self.assertNotEqual(self.workspace.checkpoint("main")["commit"], result["commit"])
        exported = self.root / "recovered.luau"
        self.workspace.export_version(digest("return 1\n"), exported)
        self.assertEqual(exported.read_text(), "return 1\n")

    def test_http_auth_and_no_public_source_endpoint(self):
        server = make_server(self.workspace)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        client = build_opener(ProxyHandler({}))
        with self.assertRaises(HTTPError) as error:
            client.open(url + "/health", timeout=3)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        request = Request(url + "/health", headers={"X-Studio-Workflow-Key": self.workspace.key})
        with client.open(request, timeout=3) as response:
            self.assertTrue(json.loads(response.read())["ok"])
        with self.assertRaises(HTTPError) as error:
            client.open(Request(url + "/source", headers={"X-Studio-Workflow-Key": self.workspace.key}), timeout=3)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()

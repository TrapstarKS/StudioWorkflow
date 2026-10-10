from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

import test_workflow as fixtures
from studioworkflow.__main__ import main
from studioworkflow.bridge import atomic_write
from studioworkflow.diagnostics import doctor


class CommandsTest(unittest.TestCase):
    record = fixtures.WorkflowTest.record
    post = fixtures.WorkflowTest.post
    snapshot = fixtures.WorkflowTest.snapshot
    local = fixtures.WorkflowTest.local

    def setUp(self):
        fixtures.WorkflowTest.setUp(self)
        self.snapshot()

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--root", str(self.root), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_read_uses_current_local_source_and_bounded_lines(self):
        self.local("local edit\nsecond\nthird\nfourth\n")
        code, stdout, stderr = self.invoke("read", "--place", "main", "--path", self.path,
                                         "--start", "2", "--lines", "2")
        self.assertEqual((code, stderr), (0, ""))
        data = json.loads(stdout)
        self.assertEqual(data["lines"], [{"line": 2, "text": "second\n"}, {"line": 3, "text": "third\n"}])
        self.assertTrue(data["truncated"])
        self.assertEqual(data["next_line"], 4)
        self.assertEqual(data["status"], {"main": {"state": "ready"}})

    def test_read_caps_a_single_large_line_and_reports_partial_line(self):
        self.local("x" * 10000)
        data = self.workspace.read_source("main", self.path, max_chars=100)
        self.assertEqual(len(data["lines"][0]["text"]), 100)
        self.assertEqual(data["partial_line"], 1)
        self.assertTrue(data["truncated"])

    def test_read_rejects_unknown_paths_and_bad_limits(self):
        for place, path, start, lines, maximum in (("missing", self.path, 1, 60, 100),
                                                  ("main", "../secret", 1, 60, 100),
                                                  ("main", "untracked.luau", 1, 60, 100),
                                                  ("main", self.path, 0, 60, 100),
                                                  ("main", self.path, 1, 201, 100),
                                                  ("main", self.path, 1, 60, 64001)):
            with self.subTest(path=path, start=start, lines=lines), self.assertRaises(ValueError):
                self.workspace.read_source(place, path, start, lines, maximum)

    def test_search_and_error_are_json(self):
        code, stdout, stderr = self.invoke("search", "return", "--place", "main", "--limit", "1")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(len(json.loads(stdout)["results"]), 1)
        code, stdout, stderr = self.invoke("status", "--place", "missing")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Unknown place alias", json.loads(stderr)["error"])

    def test_doctor_does_not_create_a_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            result = doctor(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_missing_workspace_does_not_print_traceback_or_create_state(self):
        with tempfile.TemporaryDirectory() as directory:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(["--root", directory, "search", "hello"])
            self.assertEqual(code, 1)
            self.assertIn("run studioworkflow init", json.loads(stderr.getvalue())["error"])
            self.assertEqual(list(Path(directory).iterdir()), [])

import json
import os
from pathlib import Path
import tempfile
import unittest

from studioworkflow.config import read_config, validate_config
from studioworkflow.installation import configure


class ConfigTest(unittest.TestCase):
    def config(self):
        return {"version": 1, "installation_id": "0123456789abcdef", "port": 8647,
                "places": {"lobby": {"place_id": 123}}}

    def test_safe_defaults_and_input_not_mutated(self):
        original = self.config()
        result = validate_config(original)
        self.assertTrue(result["auto_sync"])
        self.assertEqual(result["minimum_observer_protocol"], 3)
        self.assertNotIn("auto_sync", original)

    def test_invalid_config_fails_before_state_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config()
            config["places"] = {"../outside": {"place_id": 123}}
            (root / "studio-workflow.json").write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                read_config(root)
            self.assertFalse((root / ".studio-workflow").exists())

    def test_reserved_aliases_and_duplicate_ids(self):
        for places in ({"con": {"place_id": 1}}, {"a": {"place_id": 1}, "b": {"place_id": 1}},
                       {"Lobby": {"place_id": 1}}, {"a/b": {"place_id": 1}},
                       {"lobby": {"place_id": True}}, {"lobby": {"place_id": 2**53}}):
            with self.subTest(places=places), self.assertRaises(ValueError):
                validate_config({**self.config(), "places": places})

    def test_wrong_types_nonfinite_and_unsafe_port(self):
        cases = {"version": [True, 2], "port": [0, True, "8647", 65536],
                 "installation_id": ["../abc", None], "auto_sync": ["false", 1],
                 "sync_debounce_seconds": [float("nan"), float("inf"), -1, True],
                 "heartbeat_timeout_seconds": [0, "20"], "minimum_observer_protocol": [4, True]}
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    validate_config({**self.config(), name: value})

    def test_missing_and_corrupt_configuration_are_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "run studioworkflow init"):
                read_config(directory)
            (Path(directory) / "studio-workflow.json").write_text("{")
            with self.assertRaisesRegex(ValueError, "Invalid JSON"):
                read_config(directory)

    @unittest.skipIf(os.name == "nt", "POSIX directory permissions")
    def test_setup_keeps_private_state_unreadable_to_other_users(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace, _ = configure(Path(directory) / "game", {"lobby": {"place_id": 123}},
                                     plugins_dir=Path(directory) / "plugins")
            self.assertEqual(workspace.state.stat().st_mode & 0o777, 0o700)
            self.assertEqual(workspace.key_path.stat().st_mode & 0o777, 0o600)

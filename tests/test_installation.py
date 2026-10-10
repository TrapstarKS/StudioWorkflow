import io
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from studioworkflow import installation, supervisor
from studioworkflow.bridge import atomic_write, lock_file


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "workspace"
        self.plugins = self.directory / "plugins"
        self.resources = self.directory / "resources"
        self.resources.mkdir()
        (self.resources / "observer.luau").write_text("local config = __WORKFLOW_CONFIGURATION__\n", encoding="utf-8")
        (self.resources / "skill").mkdir()
        (self.resources / "skill/SKILL.md").write_text("# StudioWorkflow\nUse the recorded command.\n", encoding="utf-8")
        resource_patch = patch.object(installation, "PACKAGE_ROOT", self.resources)
        resource_patch.start()
        self.addCleanup(resource_patch.stop)
        self.places = installation.parse_places(["lobby=123", "arena=456"])

    def configure(self, **options):
        return installation.configure(self.root, self.places, plugins_dir=self.plugins, **options)

    def test_parse_places_rejects_unsafe_or_duplicate_values(self):
        self.assertEqual(self.places, {"lobby": {"place_id": 123}, "arena": {"place_id": 456}})
        for values in ([], ["../other=123"], ["Lobby=123"], ["nul=123"], ["arena=0"],
                       ["arena=9007199254740992"], ["arena=1", "arena=2"],
                       ["arena=1", "lobby=1"], ["arena=１２３"], ["arena=-3"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                installation.parse_places(values)

    def test_configuration_preserves_identity_key_and_custom_settings(self):
        workspace, plugin = self.configure(port=12001, auto_sync=False, debounce=2.5)
        config_path = self.root / "studio-workflow.json"
        config = json.loads(config_path.read_text())
        config["heartbeat_timeout_seconds"] = 29
        config["minimum_observer_protocol"] = 1
        atomic_write(config_path, json.dumps(config))
        second, second_plugin = self.configure()
        self.assertEqual(second.key, workspace.key)
        self.assertEqual(second_plugin, plugin)
        self.assertEqual(second.config["installation_id"], workspace.config["installation_id"])
        self.assertEqual(second.config["port"], 12001)
        self.assertFalse(second.config["auto_sync"])
        self.assertEqual(second.config["sync_debounce_seconds"], 2.5)
        self.assertEqual(second.config["heartbeat_timeout_seconds"], 29)
        self.assertEqual(second.config["minimum_observer_protocol"], 3)
        record = json.loads((workspace.state / "installation.json").read_text())
        self.assertEqual(record["command"][-2:], ["--root", str(self.root)])
        self.assertEqual(record["module_directory"], str(self.resources.parent))

    def test_validation_precedes_all_mutations(self):
        for options in ({"port": 1}, {"port": True}, {"auto_sync": 1}, {"debounce": float("nan")}, {"agent": "other"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.configure(**options)
            self.assertFalse(self.root.exists())
            self.assertFalse(self.plugins.exists())
        self.plugins.write_text("not a directory")
        with self.assertRaises(ValueError):
            self.configure()
        self.assertFalse(self.root.exists())

    def test_rejects_place_changes_without_touching_config(self):
        self.configure()
        path = self.root / "studio-workflow.json"
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            installation.configure(self.root, {"other": {"place_id": 789}}, plugins_dir=self.plugins)
        self.assertEqual(path.read_bytes(), before)

    def test_xml_escapes_and_luau_delimiter_preserve_embedded_values(self):
        workspace, plugin = self.configure(port=12345)
        key = 'key<&" ]==] ]===] '
        atomic_write(workspace.key_path, key)
        workspace, plugin = self.configure()
        source = ET.parse(plugin).find(".//ProtectedString[@name='Source']").text
        match = re.search(r"JSONDecode\(\[(=*)\[(.*?)\]\1\]\)", source, re.S)
        self.assertIsNotNone(match)
        embedded = json.loads(match.group(2))
        self.assertEqual(embedded["key"], key.strip())
        self.assertEqual(embedded["places"], {"123": "lobby", "456": "arena"})
        self.assertEqual(embedded["endpoint"], "http://127.0.0.1:12345")
        self.assertEqual(embedded["controller_key"], "StudioWorkflow_" + workspace.config["installation_id"])
        self.assertNotIn("__WORKFLOW_CONFIGURATION__", source)
        self.assertNotIn("websocket_wakeup", embedded)

    def test_write_failure_rolls_back_new_config_key_and_plugin(self):
        def fail_plugin(path, content, mode=0o600):
            if path.suffix == ".rbxmx":
                raise OSError("simulated plugin failure")
            atomic_write(path, content, mode)
        with patch.object(installation, "atomic_write", side_effect=fail_plugin), self.assertRaises(OSError):
            self.configure()
        self.assertFalse((self.root / "studio-workflow.json").exists())
        self.assertFalse((self.root / ".gitignore").exists())
        self.assertFalse((self.root / ".studio-workflow/key").exists())
        self.assertFalse((self.root / ".studio-workflow/installation.json").exists())

    def test_database_failure_restores_previous_installation(self):
        workspace, plugin = self.configure()
        paths = [self.root / "studio-workflow.json", self.root / ".gitignore", workspace.key_path, plugin, workspace.state / "installation.json"]
        before = {path: path.read_bytes() for path in paths}
        with patch.object(installation, "Workspace", side_effect=OSError("database failure")), self.assertRaises(OSError):
            self.configure(port=12003)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_uninstall_preserves_workspace_and_refuses_changed_plugin(self):
        workspace, plugin = self.configure()
        source = self.root / "studio/lobby/Files/example.luau"
        source.parent.mkdir(parents=True)
        source.write_text("return 1\n")
        plugin.write_text("unrelated replacement")
        with self.assertRaises(ValueError):
            installation.uninstall(workspace)
        self.assertEqual(plugin.read_text(), "unrelated replacement")
        plugin.unlink()
        installation.uninstall(workspace)
        self.assertTrue(source.exists())
        self.assertTrue(workspace.key_path.exists())
        self.assertTrue(workspace.database.exists())
        self.assertTrue((self.root / "studio-workflow.json").exists())

    def test_uninstall_rejects_recorded_path_outside_intended_directory(self):
        workspace, plugin = self.configure()
        outsider = self.directory / plugin.name
        outsider.write_bytes(plugin.read_bytes())
        record_path = workspace.state / "installation.json"
        record = json.loads(record_path.read_text())
        record["plugin"] = str(outsider)
        atomic_write(record_path, json.dumps(record))
        with self.assertRaises(ValueError):
            installation.uninstall(workspace)
        self.assertTrue(plugin.exists())
        self.assertTrue(outsider.exists())

    def test_uninstall_tolerates_missing_record(self):
        workspace, plugin = self.configure()
        (workspace.state / "installation.json").unlink()
        installation.uninstall(workspace)
        self.assertTrue(plugin.exists())
        self.assertTrue(workspace.key_path.exists())

    def test_agent_installation_is_explicit_idempotent_and_preserves_instructions(self):
        self.root.mkdir()
        instructions = self.root / "AGENTS.md"
        instructions.write_text("# Existing rules\nKeep these.\n")
        self.configure()
        self.assertFalse((self.root / ".agents/skills/studioworkflow").exists())
        self.configure(agent="both")
        first = instructions.read_text()
        self.configure(agent="both")
        self.assertEqual(instructions.read_text(), first)
        self.assertIn("Keep these.", first)
        self.assertEqual(first.count(installation.INSTRUCTIONS_START), 1)
        for directory in (".agents", ".claude"):
            self.assertEqual((self.root / directory / "skills/studioworkflow/SKILL.md").read_text(),
                             (self.resources / "skill/SKILL.md").read_text())

    def test_agent_installation_refuses_unrelated_skill_before_configuring(self):
        directory = self.root / ".agents/skills/studioworkflow"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("# Unrelated skill\n")
        with self.assertRaises(ValueError):
            self.configure(agent="codex")
        self.assertEqual((directory / "SKILL.md").read_text(), "# Unrelated skill\n")
        self.assertFalse((self.root / "studio-workflow.json").exists())

    def test_macos_startup_uses_module_and_clone_environment(self):
        workspace, plugin = self.configure()
        with patch.object(installation.sys, "platform", "darwin"), patch.object(Path, "home", return_value=self.directory), \
                patch.object(installation.os, "getuid", return_value=501, create=True), \
                patch.object(installation.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as execute:
            path = installation.install_startup(workspace)
            definition = plistlib.loads(path.read_bytes())
            self.assertEqual(definition["ProgramArguments"], [installation.sys.executable, "-m", "studioworkflow", "--root", str(self.root), "run"])
            self.assertEqual(definition["WorkingDirectory"], str(self.root))
            self.assertIn(str(self.resources.parent), definition["EnvironmentVariables"]["PYTHONPATH"])
            self.assertEqual(execute.call_args.args[0][:2], ["launchctl", "bootstrap"])
            installation.remove_startup(workspace)
            self.assertFalse(path.exists())
        self.assertTrue(plugin.exists())

    def test_startup_failure_rolls_back_files_and_record(self):
        workspace, plugin = self.configure()
        record_path = workspace.state / "installation.json"
        before = record_path.read_bytes()
        def fail_bootstrap(arguments, **options):
            if arguments[1] == "bootstrap":
                raise subprocess.CalledProcessError(1, arguments)
            return SimpleNamespace(returncode=1)
        with patch.object(installation.sys, "platform", "darwin"), patch.object(Path, "home", return_value=self.directory), \
                patch.object(installation.os, "getuid", return_value=501, create=True), \
                patch.object(installation.subprocess, "run", side_effect=fail_bootstrap), self.assertRaises(subprocess.CalledProcessError):
            installation.install_startup(workspace)
        self.assertEqual(record_path.read_bytes(), before)
        self.assertEqual(list((self.directory / "Library/LaunchAgents").glob("*.plist")), [])

    def test_windows_startup_escapes_command_and_preserves_workspace(self):
        self.root = self.directory / "workspace with space & name"
        workspace, plugin = self.configure()
        with patch.object(installation.sys, "platform", "win32"), patch.dict(installation.os.environ, {"APPDATA": str(self.directory / "app data")}), \
                patch.object(installation.subprocess, "run") as execute:
            path = installation.install_startup(workspace)
            launcher = path.read_text()
            self.assertIn('shell.CurrentDirectory = "' + str(self.root).replace('"', '""') + '"', launcher)
            command = subprocess.list2cmdline([installation.sys.executable, "-m", "studioworkflow", "--root", str(self.root), "run"])
            self.assertIn('shell.Run "' + command.replace('"', '""') + '", 0, False', launcher)
            self.assertIn('("PYTHONPATH")', launcher)
            execute.assert_called_once_with(["wscript.exe", str(path)], check=True)
            installation.uninstall(workspace)
            self.assertFalse(path.exists())
            self.assertFalse(plugin.exists())
            self.assertTrue(workspace.key_path.exists())

    def test_windows_launcher_handles_unicode_and_control_characters_as_ascii(self):
        value = 'a"é\n😀'
        literal = installation._vbs_string(value)
        self.assertTrue(literal.isascii())
        self.assertEqual(literal, '"a""" & ChrW(233) & ChrW(10) & ChrW(55357) & ChrW(56832)')

    def test_gitignore_preserves_existing_content_and_is_idempotent(self):
        self.root.mkdir()
        path = self.root / ".gitignore"
        original = b"# Game ignores\r\n*.tmp\r\n!important.tmp\r\n\r\n"
        path.write_bytes(original)
        workspace, plugin = self.configure()
        first = path.read_bytes()
        self.configure()
        self.assertEqual(path.read_bytes(), first)
        self.assertTrue(first.startswith(original))
        content = first.decode()
        self.assertEqual(content.count(installation.GITIGNORE_START), 1)
        self.assertIn("/studio-workflow.json\n/.studio-workflow/\n/studio/\n", content)
        self.assertNotIn(plugin.name, content)

    def test_gitignore_covers_local_plugin_with_literal_path(self):
        directory = self.root / "Plugins with [brackets]#!"
        workspace, plugin = installation.configure(self.root, self.places, plugins_dir=directory)
        expected = "/Plugins\\ with\\ \\[brackets\\]\\#\\!/" + plugin.name
        self.assertIn(expected, (self.root / ".gitignore").read_text().splitlines())

    def test_gitignore_validation_precedes_installation(self):
        self.root.mkdir()
        path = self.root / ".gitignore"
        path.mkdir()
        with self.assertRaises(ValueError):
            self.configure()
        self.assertFalse((self.root / "studio-workflow.json").exists())
        self.assertFalse((self.root / ".studio-workflow").exists())
        self.assertFalse(self.plugins.exists())

    def test_gitignore_symlink_is_rejected_without_changing_its_target(self):
        self.root.mkdir()
        target = self.directory / "outside.gitignore"
        target.write_text("# Preserve this\n")
        try:
            (self.root / ".gitignore").symlink_to(target)
        except (NotImplementedError, OSError):
            self.skipTest("This runner cannot create symlinks")
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(target.read_text(), "# Preserve this\n")
        self.assertFalse((self.root / "studio-workflow.json").exists())

    def test_gitignore_existing_bytes_are_restored_when_plugin_write_fails(self):
        self.root.mkdir()
        path = self.root / ".gitignore"
        original = b"# Game rules\r\n*.tmp\r\n# Last rule without newline"
        path.write_bytes(original)
        def fail_plugin(destination, content, mode=0o600):
            if destination.suffix == ".rbxmx":
                raise OSError("simulated plugin failure")
            atomic_write(destination, content, mode)
        with patch.object(installation, "atomic_write", side_effect=fail_plugin), self.assertRaises(OSError):
            self.configure()
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / "studio-workflow.json").exists())

    def test_gitignore_incomplete_owned_block_is_rejected(self):
        self.root.mkdir()
        path = self.root / ".gitignore"
        original = installation.GITIGNORE_START + "\n# incomplete\n"
        path.write_text(original)
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(path.read_text(), original)
        self.assertFalse((self.root / "studio-workflow.json").exists())


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / ".studio-workflow"
        self.state.mkdir()
        self.workspace = SimpleNamespace(root=self.root, state=self.state)

    def test_lock_prevents_a_second_supervisor(self):
        with (self.state / "supervisor.lock").open("a+b") as stream:
            lock_file(stream, blocking=False)
            with patch.object(supervisor.subprocess, "Popen") as spawn:
                self.assertFalse(supervisor.run(self.workspace))
            spawn.assert_not_called()

    def test_stop_file_terminates_child_and_restores_signals(self):
        child = Mock(pid=76543, stdout=io.BytesIO(b"server output\n"))
        child.poll.return_value = None
        def spawn(*arguments, **options):
            (self.state / "supervisor.stop").write_text("stop")
            return child
        with patch.object(supervisor.subprocess, "Popen", side_effect=spawn) as start, \
                patch.object(supervisor, "terminate") as terminate, patch.object(supervisor.signal, "signal") as signals:
            self.assertTrue(supervisor.run(self.workspace))
        self.assertEqual(start.call_args.args[0], [supervisor.sys.executable, "-m", "studioworkflow", "--root", str(self.root), "serve"])
        self.assertIn("PYTHONPATH", start.call_args.kwargs["env"])
        self.assertEqual(start.call_args.kwargs["cwd"], self.root)
        terminate.assert_called_once_with(child)
        child.wait.assert_called_once_with(timeout=8)
        self.assertEqual(signals.call_count, 4)
        self.assertFalse((self.state / "supervisor.stop").exists())
        self.assertEqual((self.state / "serve.log").read_bytes(), b"server output\n")

    def test_rotating_logs_stay_bounded(self):
        path = self.state / "serve.log"
        with patch.object(supervisor, "MAX_LOG_BYTES", 16):
            log = supervisor.RotatingLog(path)
            log.write(b"1234567890")
            log.write(b"abcdefghij")
            log.write(b"x" * 40)
            self.assertLessEqual(path.stat().st_size, 16)
            self.assertLessEqual(path.with_name("serve.previous.log").stat().st_size, 16)
        self.assertEqual(len(list(self.state.glob("*.log"))), 2)

    def test_drain_logs_short_diagnostics_before_pipe_closes(self):
        read_descriptor, write_descriptor = os.pipe()
        reader = os.fdopen(read_descriptor, "rb")
        writer = os.fdopen(write_descriptor, "wb")
        observed = threading.Event()
        received = []
        def record(content):
            received.append(content)
            observed.set()
        log = SimpleNamespace(write=record)
        thread = threading.Thread(target=supervisor._drain, args=(reader, log), daemon=True)
        thread.start()
        try:
            writer.write(b"short diagnostic\n")
            writer.flush()
            self.assertTrue(observed.wait(timeout=2), "Diagnostic stayed buffered until EOF")
            self.assertFalse(writer.closed)
            self.assertEqual(b"".join(received), b"short diagnostic\n")
        finally:
            writer.close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(reader.closed)

    def test_child_exit_waits_before_restart(self):
        first = Mock(pid=76543, stdout=io.BytesIO())
        first.poll.return_value = 1
        first.returncode = 1
        second = Mock(pid=76544, stdout=io.BytesIO())
        second.poll.return_value = None
        clock = [0]
        starts = []
        def spawn(*arguments, **options):
            starts.append(clock[0])
            if len(starts) == 2:
                (self.state / "supervisor.stop").write_text("stop")
                return second
            return first
        original_wait = supervisor.threading.Event.wait
        def wait(event, timeout=None):
            if timeout == supervisor.POLL_INTERVAL:
                clock[0] += 1
                return False
            return original_wait(event, timeout)
        with patch.object(supervisor.subprocess, "Popen", side_effect=spawn), \
                patch.object(supervisor, "terminate") as terminate, \
                patch.object(supervisor.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(supervisor.threading.Event, "wait", autospec=True, side_effect=wait):
            self.assertTrue(supervisor.run(self.workspace))
        self.assertEqual(starts, [0, 6])
        self.assertEqual(terminate.call_args_list[0].args, (first,))
        self.assertEqual(terminate.call_args_list[1].args, (second,))

    def test_forced_windows_termination_cleans_process_tree(self):
        child = Mock(pid=76543)
        child.poll.return_value = None
        with patch.object(supervisor.os, "name", "nt"), patch.object(supervisor.subprocess, "run") as execute:
            supervisor.terminate(child, force=True)
        self.assertEqual(execute.call_args.args[0], ["taskkill", "/PID", "76543", "/T", "/F"])
        child.kill.assert_called_once_with()

    def test_posix_termination_targets_process_group(self):
        child = SimpleNamespace(pid=76543)
        with patch.object(supervisor.os, "name", "posix"), patch.object(supervisor.os, "killpg", create=True) as kill, \
                patch.object(supervisor.signal, "SIGKILL", 9, create=True):
            supervisor.terminate(child)
            supervisor.terminate(child, force=True)
        self.assertEqual(kill.call_args_list[0].args, (76543, supervisor.signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (76543, 9))


if __name__ == "__main__":
    unittest.main()

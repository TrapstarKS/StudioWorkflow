"""Install an installation-scoped Studio plugin and optional login launcher."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import socket
import subprocess
import sys
from types import SimpleNamespace
import uuid
import xml.etree.ElementTree as ET

from .bridge import Workspace, atomic_write, read_text
from .config import CONFIG_NAME, read_config, validate_config, validate_places


PACKAGE_ROOT = Path(__file__).resolve().parent
SKILL_MARKER = "StudioWorkflow managed skill\n"
INSTRUCTIONS_START = "<!-- studioworkflow:start -->"
INSTRUCTIONS_END = "<!-- studioworkflow:end -->"
GITIGNORE_START = "# studioworkflow:start"
GITIGNORE_END = "# studioworkflow:end"


def parse_places(values):
    places = {}
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Use alias=PLACE_ID for every place")
        name, separator, number = value.strip().partition("=")
        if not separator or not re.fullmatch(r"[0-9]+", number):
            raise ValueError("Use a safe lowercase alias and positive Place ID: lobby=123456")
        if name in places:
            raise ValueError("Duplicate place alias")
        places[name] = {"place_id": int(number)}
    return validate_places(places)


def plugin_directory():
    if sys.platform == "darwin":
        settings = Path.home() / "Library/Roblox/GlobalSettings_13.xml"
        fallback = Path.home() / "Documents/Roblox/Plugins"
    elif sys.platform == "win32":
        local_data = os.environ.get("LOCALAPPDATA")
        if not local_data:
            raise ValueError("LOCALAPPDATA is unavailable; provide --plugins-dir")
        settings = Path(local_data) / "Roblox/GlobalSettings_13.xml"
        fallback = Path(local_data) / "Roblox/Plugins"
    else:
        raise ValueError("Studio plugin discovery supports macOS and Windows; provide --plugins-dir")
    if settings.exists():
        try:
            node = ET.parse(settings).find(".//QDir[@name='PluginsDir']")
        except ET.ParseError as error:
            raise ValueError(f"Invalid Roblox settings XML: {settings}") from error
        if node is not None and node.text and node.text.strip():
            return Path(os.path.expandvars(node.text.strip())).expanduser()
    return fallback


def free_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def _plugin_name(identity):
    return f"StudioWorkflow-{identity}.rbxmx"


def _plugin_xml(config, key):
    source = (PACKAGE_ROOT / "observer.luau").read_text(encoding="utf-8")
    if source.count("__WORKFLOW_CONFIGURATION__") != 1:
        raise ValueError("Observer template must contain exactly one configuration token")
    embedded = {
        "endpoint": f"http://127.0.0.1:{config['port']}",
        "key": key,
        "places": {str(place["place_id"]): name for name, place in config["places"].items()},
        "controller_key": "StudioWorkflow_" + config["installation_id"],
    }
    encoded = json.dumps(embedded, ensure_ascii=True)
    delimiter = "=="
    while f"]{delimiter}]" in encoded:
        delimiter += "="
    expression = f"HttpService:JSONDecode([{delimiter}[{encoded}]{delimiter}])"
    source = source.replace("__WORKFLOW_CONFIGURATION__", expression)
    model = ET.Element("roblox", version="4")
    instance = ET.SubElement(model, "Item", {"class": "Script", "referent": "RBX0"})
    properties = ET.SubElement(instance, "Properties")
    ET.SubElement(properties, "string", name="Name").text = "StudioWorkflow"
    ET.SubElement(properties, "ProtectedString", name="Source").text = source
    ET.SubElement(properties, "bool", name="Disabled").text = "false"
    output = ET.tostring(model, encoding="unicode")
    ET.fromstring(output)
    return output


def _read_record(workspace):
    path = workspace.state / "installation.json"
    if not path.exists():
        return {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid installation record; no installed files were changed") from error
    if not isinstance(record, dict):
        raise ValueError("Invalid installation record")
    return record


def _record_matches(record, root, identity):
    if record and (record.get("root") != str(root) or record.get("installation_id") != identity):
        raise ValueError("Installation record belongs to another workspace")


def _owned_plugin(record, root, identity):
    _record_matches(record, root, identity)
    if not record.get("plugin"):
        return None
    plugin = Path(record["plugin"])
    directory = Path(record.get("plugin_directory", ""))
    if not plugin.is_absolute() or not directory.is_absolute():
        raise ValueError("Invalid plugin ownership record")
    expected = directory / _plugin_name(identity)
    if plugin != expected or plugin.is_symlink() or directory.resolve() != directory:
        raise ValueError("Plugin path differs from its recorded installation destination")
    if plugin.exists():
        actual = hashlib.sha256(plugin.read_bytes()).hexdigest()
        if actual != record.get("plugin_sha256"):
            raise ValueError("Installed plugin was replaced or changed; refusing to overwrite or delete it")
    return plugin


def _agent_files(root, agent):
    if agent is None:
        return {}
    if agent not in {"codex", "claude", "both"}:
        raise ValueError("Agent must be codex, claude or both")
    skill = (PACKAGE_ROOT / "skill/SKILL.md").read_text(encoding="utf-8")
    changes = {}
    for name, document in (("codex", "AGENTS.md"), ("claude", "CLAUDE.md")):
        if agent not in {name, "both"}:
            continue
        directory = root / (".agents" if name == "codex" else ".claude") / "skills/studioworkflow"
        marker = directory / ".studioworkflow-owned"
        if directory.exists() and (not marker.is_file() or marker.read_text(encoding="utf-8") != SKILL_MARKER):
            raise ValueError(f"An unrelated skill already exists at {directory}")
        if directory.is_symlink() or not directory.resolve().is_relative_to(root):
            raise ValueError("Agent skill directory escapes the workspace")
        changes[directory / "SKILL.md"] = skill
        changes[marker] = SKILL_MARKER
        path = root / document
        if path.is_symlink():
            raise ValueError("Agent instructions must be a workspace file")
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        block = (f"{INSTRUCTIONS_START}\n"
                 "For Roblox Studio source work in this workspace, read "
                 f"{'.agents' if name == 'codex' else '.claude'}/skills/studioworkflow/SKILL.md.\n"
                 f"{INSTRUCTIONS_END}")
        if content.count(INSTRUCTIONS_START) != content.count(INSTRUCTIONS_END) or content.count(INSTRUCTIONS_START) > 1:
            raise ValueError(f"Invalid StudioWorkflow instructions block in {path}")
        if INSTRUCTIONS_START in content:
            start = content.index(INSTRUCTIONS_START)
            end = content.index(INSTRUCTIONS_END, start) + len(INSTRUCTIONS_END)
            content = content[:start] + block + content[end:]
        else:
            content = content.rstrip() + ("\n\n" if content.strip() else "") + block + "\n"
        changes[path] = content
    return changes


def _gitignore_content(root, plugin):
    path = root / ".gitignore"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Workspace .gitignore must be a regular file")
    content = read_text(path) or ""
    patterns = ["/studio-workflow.json", "/.studio-workflow/", "/studio/"]
    if plugin.is_relative_to(root):
        relative = plugin.relative_to(root).as_posix()
        patterns.append("/" + re.sub(r"([\\*?\[\]#! ])", r"\\\1", relative))
    block = "\n".join([GITIGNORE_START, *patterns, GITIGNORE_END])
    if content.count(GITIGNORE_START) != content.count(GITIGNORE_END) or content.count(GITIGNORE_START) > 1:
        raise ValueError("Invalid StudioWorkflow block in workspace .gitignore")
    if GITIGNORE_START in content:
        start = content.index(GITIGNORE_START)
        try:
            end = content.index(GITIGNORE_END, start) + len(GITIGNORE_END)
        except ValueError as error:
            raise ValueError("Invalid StudioWorkflow block in workspace .gitignore") from error
        return content[:start] + block + content[end:]
    separator = ("\n" if content.endswith("\n") else "\n\n") if content else ""
    return content + separator + block + "\n"


def _write_transaction(changes, after=None):
    previous = {}
    for path in changes:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"Installation destination is not a regular file: {path}")
        previous[path] = (read_text(path), path.stat().st_mode & 0o777) if path.exists() else None
    try:
        for path, content in changes.items():
            atomic_write(path, content)
        return after() if after is not None else None
    except BaseException:
        for path, old in reversed(list(previous.items())):
            try:
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, old[0], old[1])
            except OSError:
                pass
        raise


def configure(root, places, *, port=None, plugins_dir=None, auto_sync=None, debounce=None, agent=None):
    root = Path(root).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError("Workspace root must be a directory")
    places = validate_places(places)
    config_path = root / CONFIG_NAME
    old = read_config(root) if config_path.exists() else {}
    if old and old["places"] != places:
        raise ValueError("This workspace already belongs to different places; use a new workspace")
    config = dict(old) if old else {
        "version": 1, "port": free_port() if port is None else port,
        "installation_id": uuid.uuid4().hex[:16], "places": places,
    }
    for name, value in (("port", port), ("auto_sync", auto_sync), ("sync_debounce_seconds", debounce)):
        if value is not None:
            config[name] = value
    config = validate_config(config)
    config["minimum_observer_protocol"] = 3
    state = root / ".studio-workflow"
    if not state.resolve().is_relative_to(root):
        raise ValueError("Workspace state directory escapes the workspace")
    key_path = state / "key"
    key = key_path.read_text(encoding="utf-8").strip() if key_path.exists() else secrets.token_urlsafe(32)
    if not key or len(key) > 1024 or any(ord(character) < 32 for character in key):
        raise ValueError("Invalid workspace authentication key")
    record_path = state / "installation.json"
    temporary_workspace = SimpleNamespace(root=root, state=state)
    record = _read_record(temporary_workspace)
    owned = _owned_plugin(record, root, config["installation_id"])
    directory = Path(plugins_dir) if plugins_dir is not None else (
        owned.parent if owned is not None else plugin_directory())
    directory = directory.expanduser().resolve()
    if directory.exists() and not directory.is_dir():
        raise ValueError("Plugin destination must be a directory")
    plugin = directory / _plugin_name(config["installation_id"])
    if plugin.exists() and plugin != owned:
        raise ValueError("Plugin destination already contains an unowned file")
    source = _plugin_xml(config, key)
    next_record = {**record, "root": str(root), "installation_id": config["installation_id"],
                   "plugin": str(plugin), "plugin_directory": str(directory),
                   "plugin_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                   "python_executable": sys.executable, "module_directory": str(PACKAGE_ROOT.parent),
                   "command": [sys.executable, "-m", "studioworkflow", "--root", str(root)]}
    changes = _agent_files(root, agent)
    changes.update({root / ".gitignore": _gitignore_content(root, plugin),
                    config_path: json.dumps(config, indent=2) + "\n", key_path: key,
                    plugin: source, record_path: json.dumps(next_record, indent=2) + "\n"})
    database = state / "index.sqlite3"
    database_existed = database.exists()
    try:
        workspace = _write_transaction(changes, after=lambda: Workspace(root))
    except BaseException:
        if not database_existed:
            for suffix in ("", "-wal", "-shm"):
                Path(str(database) + suffix).unlink(missing_ok=True)
        raise
    if owned is not None and owned != plugin:
        owned.unlink(missing_ok=True)
    return workspace, plugin


def module_environment():
    environment = dict(os.environ)
    tool_root = str(PACKAGE_ROOT.parent)
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = tool_root + (os.pathsep + existing if existing else "")
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _startup_path(workspace):
    identity = workspace.config["installation_id"]
    if sys.platform == "darwin":
        return Path.home() / "Library/LaunchAgents" / f"local.studioworkflow.{identity}.plist"
    if sys.platform == "win32":
        app_data = os.environ.get("APPDATA")
        if not app_data:
            raise ValueError("APPDATA is unavailable; cannot configure login startup")
        return Path(app_data) / "Microsoft/Windows/Start Menu/Programs/Startup" / f"StudioWorkflow-{identity}.vbs"
    raise ValueError("Login startup supports macOS and Windows")


def _vbs_string(value):
    fragments = []
    literal = ""
    for character in str(value):
        if 32 <= ord(character) <= 126:
            literal += character.replace('"', '""')
            continue
        if literal:
            fragments.append('"' + literal + '"')
            literal = ""
        encoded = character.encode("utf-16-le")
        for offset in range(0, len(encoded), 2):
            fragments.append(f"ChrW({int.from_bytes(encoded[offset:offset + 2], 'little')})")
    if literal or not fragments:
        fragments.append('"' + literal + '"')
    return " & ".join(fragments)


def _launcher_source(workspace):
    arguments = [sys.executable, "-m", "studioworkflow", "--root", str(workspace.root), "run"]
    if sys.platform == "darwin":
        return plistlib.dumps({
            "Label": f"local.studioworkflow.{workspace.config['installation_id']}",
            "ProgramArguments": arguments, "WorkingDirectory": str(workspace.root),
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
            "EnvironmentVariables": {"PYTHONPATH": module_environment()["PYTHONPATH"], "PYTHONUNBUFFERED": "1"},
            "StandardOutPath": str(workspace.state / "launcher.log"),
            "StandardErrorPath": str(workspace.state / "launcher-error.log"),
        }).decode("utf-8")
    command = subprocess.list2cmdline(arguments)
    return ("Set shell = CreateObject(\"WScript.Shell\")\n"
            f"shell.CurrentDirectory = {_vbs_string(workspace.root)}\n"
            f"shell.Environment(\"PROCESS\")(\"PYTHONPATH\") = {_vbs_string(module_environment()['PYTHONPATH'])}\n"
            f"shell.Run {_vbs_string(command)}, 0, False\n")


def install_startup(workspace):
    intended = _startup_path(workspace).expanduser()
    if intended.is_symlink():
        raise ValueError("Startup destination must be a regular file")
    destination = intended.resolve()
    record = _read_record(workspace)
    _record_matches(record, workspace.root, workspace.config["installation_id"])
    source = _launcher_source(workspace)
    if destination.exists() and str(destination) != record.get("startup"):
        raise ValueError("Startup destination already contains an unowned file")
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != record.get("startup_sha256"):
        raise ValueError("Startup launcher was replaced or changed; refusing to overwrite it")
    next_record = {**record, "root": str(workspace.root), "installation_id": workspace.config["installation_id"],
                   "startup": str(destination), "startup_platform": sys.platform,
                   "startup_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()}
    def activate():
        if sys.platform == "darwin":
            domain = f"gui/{os.getuid()}"
            service = domain + f"/local.studioworkflow.{workspace.config['installation_id']}"
            if subprocess.run(["launchctl", "print", service], capture_output=True).returncode == 0:
                subprocess.run(["launchctl", "bootout", service], check=True)
            subprocess.run(["launchctl", "bootstrap", domain, str(destination)], check=True)
        else:
            subprocess.run(["wscript.exe", str(destination)], check=True)
    _write_transaction({destination: source, workspace.state / "installation.json": json.dumps(next_record, indent=2) + "\n"}, after=activate)
    return destination


def remove_startup(workspace):
    record = _read_record(workspace)
    _record_matches(record, workspace.root, workspace.config["installation_id"])
    atomic_write(workspace.state / "supervisor.stop", workspace.config["installation_id"])
    if not record.get("startup"):
        return
    destination = Path(record["startup"])
    if record.get("startup_platform") != sys.platform or destination != _startup_path(workspace).expanduser().resolve() or destination.is_symlink():
        raise ValueError("Startup path differs from its recorded installation destination")
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != record.get("startup_sha256"):
        raise ValueError("Startup launcher was replaced or changed; refusing to delete it")
    if sys.platform == "darwin":
        service = f"gui/{os.getuid()}/local.studioworkflow.{workspace.config['installation_id']}"
        if subprocess.run(["launchctl", "print", service], capture_output=True).returncode == 0:
            subprocess.run(["launchctl", "bootout", service], check=True)
    destination.unlink(missing_ok=True)
    for name in ("startup", "startup_platform", "startup_sha256"):
        record.pop(name, None)
    atomic_write(workspace.state / "installation.json", json.dumps(record, indent=2) + "\n")


def uninstall(workspace):
    record = _read_record(workspace)
    plugin = _owned_plugin(record, workspace.root, workspace.config["installation_id"])
    remove_startup(workspace)
    if plugin is not None:
        plugin.unlink(missing_ok=True)
    record = _read_record(workspace)
    for name in ("plugin", "plugin_directory", "plugin_sha256"):
        record.pop(name, None)
    if record:
        atomic_write(workspace.state / "installation.json", json.dumps(record, indent=2) + "\n")

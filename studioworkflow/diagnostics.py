"""Inspect the installation without creating state or changing a game."""

import json
from pathlib import Path
import shutil
import sys
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from .bridge import Workspace
from .config import read_config


def doctor(root):
    root = Path(root).resolve()
    result = {"workspace": str(root), "ok": True, "checks": []}

    def check(name, ok, detail):
        result["checks"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            result["ok"] = False

    check("git", bool(shutil.which("git")), "Git is required for three-way merges and checkpoints")
    check("platform", sys.platform in ("darwin", "win32"), "Roblox Studio runs on macOS and Windows")
    try:
        config = read_config(root)
    except (ValueError, OSError) as error:
        check("configuration", False, str(error))
        return result
    check("configuration", True, f"{len(config['places'])} configured place(s)")
    state = root / ".studio-workflow"
    try:
        record = json.loads((state / "installation.json").read_text(encoding="utf-8"))
        check("plugin", Path(record["plugin"]).is_file(), "Re-run init if the installed plugin is missing; reopen Studio after installation")
    except (ValueError, KeyError, OSError, TypeError):
        check("plugin", False, "Installation record is missing or invalid; re-run init with the same places")
    try:
        workspace = Workspace(root, read_only=True)
        result["places"] = workspace.status()
        check("index", True, "Local source index is available")
    except (ValueError, OSError) as error:
        check("index", False, str(error))
    try:
        key = (state / "key").read_text(encoding="utf-8").strip()
        request = Request(f"http://127.0.0.1:{config['port']}/health", headers={"X-Studio-Workflow-Key": key})
        with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
            health = json.load(response)
        check("service", health.get("ok") is True, "Authenticated local server is reachable")
        result["places"] = health.get("status", result.get("places", {}))
    except (URLError, OSError, ValueError, TypeError):
        check("service", False, "Local server is unreachable or authentication failed; start run and inspect .studio-workflow/serve.log")
    places = result.get("places", {})
    if places:
        ready = any(item["state"] == "ready" for item in places.values())
        check("studio", ready, "Open a configured place in Edit mode and accept the plugin's localhost/script permissions; ready means an inventory is complete")
    return result

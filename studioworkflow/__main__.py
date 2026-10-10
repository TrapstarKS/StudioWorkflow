"""Command-line entry point for StudioWorkflow."""

import argparse
import json
from pathlib import Path
import shutil
import sys
import time

from . import __version__
from .bridge import Workspace, atomic_write


def parser():
    result = argparse.ArgumentParser(description="Search and edit Roblox Studio scripts locally.")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--root", type=Path, default=Path.cwd(), help="game workspace (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="configure workspace and install a local Studio plugin")
    init.add_argument("--place", action="append", help="alias=PLACE_ID; repeat for multiple places")
    init.add_argument("--port", type=int)
    init.add_argument("--plugins-dir", type=Path)
    init.add_argument("--auto-sync", action=argparse.BooleanOptionalAction, default=None)
    init.add_argument("--debounce", type=float, help="seconds between last save and automatic sending")
    init.add_argument("--agent", choices=("codex", "claude", "both", "none"), default="none")
    init.add_argument("--startup", action="store_true", help="start in background and at login (opt-in)")
    for name in ("serve", "run", "stop", "doctor", "uninstall"):
        commands.add_parser(name)
    status = commands.add_parser("status")
    status.add_argument("--place")
    query = commands.add_parser("search")
    query.add_argument("query")
    query.add_argument("--place")
    query.add_argument("--limit", type=int, default=8)
    query.add_argument("--full", action="store_true", help="include diagnostic details")
    query.add_argument("--observed", action="store_true", help="search the indexed Studio snapshot instead of current Files")
    read = commands.add_parser("read", help="read a bounded range from the current editable local file")
    read.add_argument("--place", required=True)
    read.add_argument("--path", required=True)
    read.add_argument("--start", type=int, default=1)
    read.add_argument("--lines", type=int, default=60)
    read.add_argument("--max-chars", type=int, default=16000)
    for name in ("changes", "checkpoint"):
        command = commands.add_parser(name)
        command.add_argument("--place", required=True)
    history = commands.add_parser("history")
    history.add_argument("--place", required=True)
    history.add_argument("--path", required=True)
    export = commands.add_parser("export-version")
    export.add_argument("hash")
    export.add_argument("--output", required=True, type=Path)
    for name in ("pause-outbound", "unpause-outbound"):
        command = commands.add_parser(name)
        command.add_argument("--place")
        command.add_argument("--reason", default="")
    for name in ("apply", "rebase", "checkout", "release", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--place", required=True)
        command.add_argument("--path", required=True)
        if name == "apply":
            command.add_argument("--wait", type=float, default=20)
    operation = commands.add_parser("operation")
    operation.add_argument("id")
    return result


def initialize(args):
    from .config import read_config
    from .installation import configure, install_startup, parse_places

    if not shutil.which("git"):
        raise ValueError("Install Git before setup; it is required for conflict merges and checkpoints")
    if args.place:
        places = parse_places(args.place)
    elif (args.root / "studio-workflow.json").exists():
        places = read_config(args.root)["places"]
    elif sys.stdin.isatty():
        values = input("Places, separated by commas (lobby=123456, arena=987654): ")
        places = parse_places(values.split(","))
    else:
        raise ValueError("Non-interactive setup needs --place alias=PLACE_ID")
    workspace, plugin = configure(args.root, places, port=args.port, plugins_dir=args.plugins_dir,
                                  auto_sync=args.auto_sync, debounce=args.debounce,
                                  agent=None if args.agent == "none" else args.agent)
    result = {"workspace": str(workspace.root), "plugin": str(plugin),
              "restart_studio_required": True, "places": list(places),
              "next": "Reopen Studio, open a configured place in Edit mode, and run doctor"}
    if args.startup:
        result["startup"] = str(install_startup(workspace))
    else:
        result["next"] = "Start studioworkflow run, reopen Studio, then run doctor"
    return result


def execute(args):
    if args.command == "init":
        return initialize(args)
    if args.command == "doctor":
        from .diagnostics import doctor
        return doctor(args.root)
    read_only = args.command in ("status", "search", "read", "changes", "history", "operation", "export-version")
    workspace = Workspace(args.root, read_only=read_only)
    if getattr(args, "place", None) and args.place not in workspace.config["places"]:
        raise ValueError("Unknown place alias; use a name from studio-workflow.json")
    if args.command == "serve":
        from .bridge import serve
        serve(workspace)
        return
    if args.command == "run":
        from .supervisor import run
        run(workspace)
        return
    if args.command == "stop":
        atomic_write(workspace.state / "supervisor.stop", workspace.config["installation_id"])
        return {"stop_requested": True, "note": "Use uninstall to remove login startup; a configured service manager may restart the server"}
    if args.command == "uninstall":
        from .installation import uninstall
        uninstall(workspace)
        return {"uninstalled": True, "note": "Plugin and login startup removed; local files and history preserved. Reopen Studio to unload the plugin."}
    if args.command == "status":
        return workspace.status(args.place)
    if args.command == "search":
        return workspace.query(args.query, args.place, args.limit, full=args.full, observed=args.observed)
    if args.command == "read":
        return workspace.read_source(args.place, args.path, args.start, args.lines, args.max_chars)
    if args.command in ("pause-outbound", "unpause-outbound"):
        return workspace.set_outbound_paused(args.command == "pause-outbound", args.reason, args.place)
    if args.command == "operation":
        return workspace.operation(args.id)
    if args.command == "export-version":
        return workspace.export_version(args.hash, args.output)
    if args.command == "apply":
        result = workspace.queue_apply(args.place, args.path)
        deadline = time.monotonic() + min(max(args.wait, 0), 55)
        while result["state"] in ("queued", "delivered") and time.monotonic() < deadline:
            time.sleep(0.2)
            result = workspace.operation(result["id"])
        return result
    method = getattr(workspace, args.command.replace("-", "_"))
    if getattr(args, "path", None) is not None:
        return method(args.place, args.path)
    return method(args.place)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "doctor" and not result["ok"]:
            return 1
        if args.command == "apply" and result["state"] not in ("applied", "unchanged"):
            return 1
        if args.command == "rebase" and result.get("action") in ("conflict", "deleted_in_studio", "local_delete_requires_explicit_studio_action"):
            return 1
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

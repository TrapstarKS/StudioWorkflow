#!/usr/bin/env python3
"""Exact Studio source files, local search and conflict-checked script updates."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .websocket_wakeup import Wakeups
from .config import read_config
from .config import RESERVED_NAMES

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


PROJECT = Path.cwd()
MAX_BODY = 768 * 1024
OPERATION_CHUNK_CHARACTERS = 32000
MAINTENANCE_BATCH_SIZE = 16
OPERATION_TIMEOUT_SECONDS = 30
MAX_RECORD_BYTES = 16_000_000
MAX_RECORD_CHUNKS = 100
RECORD_CHUNK_BYTES = 200_000
MAX_OPERATION_CHUNKS = 1000
TEMPORARY_READY_ERROR = "Studio is offline, playing or still refreshing; writes are blocked"


class WorkspaceUnavailable(ValueError):
    """A temporary delivery gate, not a source conflict."""


def lock_file(stream, *, blocking=True):
    if fcntl is not None:
        operation = fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        fcntl.flock(stream, operation)
        return

    if msvcrt is None:
        raise RuntimeError("No supported file-lock implementation")

    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
    stream.seek(0)
    operation = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
    try:
        msvcrt.locking(stream.fileno(), operation, 1)
    except OSError as error:
        if not blocking:
            raise BlockingIOError(str(error)) from error
        raise


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("Path must stay inside its configured directory")
    for segment in value.split("/"):
        if segment.endswith((" ", ".")) or segment.split(".")[0].lower() in RESERVED_NAMES:
            raise ValueError("Path contains a Windows reserved or aliased filename")
        if any(ord(character) < 32 or character in ':<>"|?*' for character in segment):
            raise ValueError("Path contains a nonportable filename character")
    return str(path)


def inside(root: Path, relative: str) -> Path:
    path = root / safe_relative(relative)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Symlink escapes configured directory")
    return path


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".workflow-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode if os.name != "nt" else 0o600)
        if os.name == "nt" and path.exists():
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_text(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


def reconcile(base: str | None, local: str | None, remote: str | None) -> dict:
    if local == remote:
        return {"action": "equal", "content": local}
    if local == base:
        return {"action": "take_studio", "content": remote}
    if remote == base:
        return {"action": "keep_local", "content": local}
    if None in (base, local, remote):
        return {"action": "conflict", "reason": "Concurrent creation, deletion or edit"}
    with tempfile.TemporaryDirectory(prefix="studioworkflow-merge-") as directory:
        paths = [Path(directory) / name for name in ("local", "base", "studio")]
        for path, content in zip(paths, (local, base, remote)):
            path.write_text(content, encoding="utf-8", newline="")
        process = subprocess.run(
            ["git", "merge-file", "-p", "-L", "local", "-L", "baseline", "-L", "Studio",
             *(str(path) for path in paths)], capture_output=True, timeout=10,
        )
    if process.returncode == 0:
        return {"action": "merge", "content": process.stdout.decode("utf-8")}
    if 0 < process.returncode < 128:
        return {"action": "conflict", "reason": "Overlapping edits", "proposal": process.stdout.decode("utf-8")}
    raise RuntimeError(process.stderr.decode("utf-8").strip() or "Three-way merge failed")


class Workspace:
    def __init__(self, root: Path = PROJECT, *, read_only: bool = False):
        self.root = root.resolve()
        self.config = read_config(self.root)
        self.state = self.root / ".studio-workflow"
        self.lock = threading.RLock()
        self.busy_activity_lock = threading.Lock()
        self.busy_activity = {}
        self.wakeup_generation = 0
        self.read_only = read_only
        self.database = self.state / "index.sqlite3"
        self.key_path = self.state / "key"

        if self.read_only:
            if not self.state.is_dir() or not self.database.is_file():
                raise WorkspaceUnavailable("Studio workflow index is unavailable; start or repair the local service")
            self.key = ""
            return

        self.state.mkdir(mode=0o700, exist_ok=True)
        self.state.chmod(0o700)
        if not self.key_path.exists():
            atomic_write(self.key_path, secrets.token_urlsafe(32))
        self.key = self.key_path.read_text().strip()
        self.ensure_schema()

    def ensure_schema(self):
        with self.connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    place TEXT PRIMARY KEY, session TEXT NOT NULL, heartbeat REAL NOT NULL,
                    complete INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'Edit',
                    expected INTEGER NOT NULL DEFAULT 0, last_snapshot REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS scripts (
                    place TEXT NOT NULL, path TEXT NOT NULL, instance_path TEXT NOT NULL,
                    source TEXT NOT NULL, hash TEXT NOT NULL, metadata TEXT NOT NULL,
                    native_path TEXT, sync_status TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY (place, path)
                );
                CREATE TABLE IF NOT EXISTS staging (
                    place TEXT NOT NULL, session TEXT NOT NULL, path TEXT NOT NULL,
                    record TEXT NOT NULL, PRIMARY KEY(place, session, path)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    place TEXT NOT NULL, session TEXT NOT NULL, transfer TEXT NOT NULL,
                    position INTEGER NOT NULL, total INTEGER NOT NULL, endpoint TEXT NOT NULL,
                    text TEXT NOT NULL, PRIMARY KEY(place,session,transfer,position)
                );
                CREATE TABLE IF NOT EXISTS baselines (
                    place TEXT NOT NULL, path TEXT NOT NULL, hash TEXT NOT NULL,
                    source TEXT NOT NULL, native_path TEXT NOT NULL, verified REAL NOT NULL,
                    PRIMARY KEY(place, path)
                );
                CREATE TABLE IF NOT EXISTS history (
                    hash TEXT PRIMARY KEY, source TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, place TEXT NOT NULL, path TEXT,
                    kind TEXT NOT NULL, at REAL NOT NULL, details TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS file_bases (
                    place TEXT NOT NULL, path TEXT NOT NULL, source TEXT NOT NULL,
                    PRIMARY KEY(place,path)
                );
                CREATE TABLE IF NOT EXISTS leases (
                    place TEXT NOT NULL, path TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(place,path)
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, place TEXT NOT NULL, session TEXT NOT NULL,
                    path TEXT NOT NULL, instance_id TEXT NOT NULL, baseline TEXT NOT NULL,
                    source TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                    result TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS autosync (
                    place TEXT NOT NULL, path TEXT NOT NULL, observed_hash TEXT,
                    changed_at REAL NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                    operation_id TEXT, PRIMARY KEY(place,path)
                );
                CREATE TABLE IF NOT EXISTS outbound_control (
                    scope TEXT PRIMARY KEY, paused INTEGER NOT NULL,
                    reason TEXT NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS file_identities (
                    place TEXT NOT NULL, path TEXT NOT NULL, session TEXT NOT NULL,
                    instance_id TEXT NOT NULL, PRIMARY KEY(place,path)
                );
                CREATE TABLE IF NOT EXISTS request_results (
                    place TEXT NOT NULL, session TEXT NOT NULL, epoch INTEGER NOT NULL,
                    request_id TEXT NOT NULL, endpoint TEXT NOT NULL, request_hash TEXT NOT NULL,
                    response TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(place,session,epoch,request_id)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
                    place UNINDEXED, path, source, tokenize='unicode61'
                );
            """)
            connection.execute("BEGIN IMMEDIATE")
            for table, additions in {
                "sessions": {"protocol_version": "INTEGER NOT NULL DEFAULT 1", "epoch": "INTEGER NOT NULL DEFAULT 0",
                             "snapshot_id": "TEXT NOT NULL DEFAULT ''"},
                "operations": {"delivered_at": "REAL", "activity_at": "REAL", "completed_at": "REAL",
                               "observed": "INTEGER NOT NULL DEFAULT 0", "epoch": "INTEGER NOT NULL DEFAULT 0"},
            }.items():
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                for name, definition in additions.items():
                    if name not in columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            connection.execute("CREATE INDEX IF NOT EXISTS operations_pending ON operations(place,state,path)")
            connection.execute("CREATE INDEX IF NOT EXISTS request_results_created ON request_results(created)")

    @contextmanager
    def connect(self):
        if self.read_only:
            connection = sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        else:
            connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def place_name(self, place_id: int) -> str:
        for name, config in self.config["places"].items():
            if config["place_id"] == place_id:
                return name
        raise ValueError("Place is not allowlisted")

    def work_file(self, place: str, relative: str) -> Path:
        if place not in self.config["places"]:
            raise ValueError("Unknown place")
        return inside(self.root / "studio" / place / "Files", relative)

    def outbound_pause(self, connection, place):
        rows = connection.execute(
            "SELECT scope,reason FROM outbound_control WHERE scope IN ('',?) AND paused=1 ORDER BY scope",
            (place,),
        ).fetchall()
        return {"paused": bool(rows), "reason": "; ".join(row["reason"] for row in rows if row["reason"])}

    def set_outbound_paused(self, paused: bool, reason: str = "", place: str | None = None):
        """Persist a global/per-place send barrier; inbound snapshots remain available.

        The SQLite write lock serializes this barrier with queueing, heartbeat delivery
        and chunk reads, including callers in another process. Already returned bytes
        cannot be recalled. Clearing a global pause preserves any per-place pauses.
        """
        if type(paused) is not bool or not isinstance(reason, str):
            raise ValueError("Pause requires a boolean and a text reason")
        if place is not None and place not in self.config["places"]:
            raise ValueError("Unknown place")
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR REPLACE INTO outbound_control VALUES(?,?,?,?)",
                               (place or "", int(paused), reason, time.time()))
            self.wakeup_generation += 1
        return {"scope": place, "outbound_paused": paused, "reason": reason}

    def invalidate_sessions(self, place: str | None = None, reason: str = "Fresh snapshot required"):
        """Invalidate mirrors under the outbound barrier, without editing source files.

        Call set_outbound_paused(True) before this method when rebuilding mirrors.
        Inbound /snapshot/start, /snapshot/batch and /snapshot/finish then establish
        a new inventory while outbound delivery remains paused. Delivered operations
        remain uncertain until an exact source/identity observation or result arrives.
        """
        places = [place] if place is not None else list(self.config["places"])
        if any(name not in self.config["places"] for name in places):
            raise ValueError("Unknown place")
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if any(not self.outbound_pause(connection, name)["paused"] for name in places):
                raise ValueError("Pause outbound delivery before invalidating sessions")
            for name in places:
                self.invalidate_inventory(connection, name, reason)
                connection.execute("UPDATE sessions SET heartbeat=0 WHERE place=?", (name,))
        return {"invalidated": places, "outbound_paused": True}

    def invalidate_inventory(self, connection, place, reason):
        connection.execute("UPDATE sessions SET complete=0,expected=-1,snapshot_id='' WHERE place=?", (place,))
        connection.execute("DELETE FROM staging WHERE place=?", (place,))
        connection.execute("DELETE FROM chunks WHERE place=?", (place,))
        connection.execute("UPDATE operations SET state='expired',completed_at=?,result=? WHERE place=? AND state='queued'",
                           (time.time(), json.dumps({"error": reason, "delivery": "not_delivered"}), place))
        connection.execute("UPDATE operations SET state='uncertain',result=? WHERE place=? AND state='delivered'",
                           (json.dumps({"error": reason, "delivery": "uncertain"}), place))

    def maintain_operations(self, connection, now, place=None):
        connection.execute("""UPDATE operations SET state='uncertain',result=?
            WHERE state='delivered' AND COALESCE(activity_at,delivered_at,created) < ?
            AND (? IS NULL OR place=?)""",
            (json.dumps({"error": "Delivery has no confirmation; waiting for a fresh observation or result",
                         "delivery": "uncertain"}), now - OPERATION_TIMEOUT_SECONDS, place, place))

    def observe_liveness(self, endpoint, payload):
        place = self.place_name(payload["place_id"])
        mode = payload.get("mode", "Edit")
        epoch = payload.get("epoch", 0)
        if type(epoch) is not int or epoch < 0:
            raise ValueError("Invalid observer epoch")
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT * FROM sessions WHERE place=? AND session=?",
                                         (place, payload["session"])).fetchone()
            if not current:
                return
            if epoch < current["epoch"]:
                raise ValueError("Stale observer epoch; start a fresh snapshot")
            if epoch > current["epoch"]:
                self.invalidate_inventory(connection, place, "Observer epoch advanced")
                connection.execute("UPDATE sessions SET epoch=? WHERE place=?", (epoch, place))
            now = time.time()
            self.maintain_operations(connection, now, place)
            expired = now - current["heartbeat"] >= self.config["heartbeat_timeout_seconds"]
            if expired and self.busy_request_keeps_session_alive(place, current, payload, now):
                expired = False
            incomplete_hint = payload.get("snapshot_incomplete") is True
            if endpoint != "/snapshot/start" and (
                expired or (mode != "Edit" and (current["complete"] or current["expected"] >= 0))
                or (incomplete_hint and current["complete"])
            ):
                self.invalidate_inventory(connection, place, "Observer went offline, entered Play or requested a fresh snapshot")
            connection.execute("UPDATE sessions SET heartbeat=?,mode=? WHERE place=?",
                               (now, mode, place))

    def note_busy_request(self, endpoint, payload):
        identity = self.request_identity(endpoint, payload)
        if not identity:
            return
        now = time.time()
        key = (identity["place"], identity["session"], identity["epoch"])
        with self.busy_activity_lock:
            previous = self.busy_activity.get(key)
            timeout = self.config["heartbeat_timeout_seconds"]
            continuous = previous and now - previous["last"] < timeout
            self.busy_activity[key] = {
                "first": previous["first"] if continuous else now,
                "last": now,
            }
            cutoff = now - 300
            self.busy_activity = {
                activity_key: activity
                for activity_key, activity in self.busy_activity.items()
                if activity["last"] >= cutoff
            }

    def busy_request_keeps_session_alive(self, place, current, payload, now):
        key = (place, current["session"], current["epoch"])
        with self.busy_activity_lock:
            activity = self.busy_activity.get(key)
        if not activity:
            return False
        timeout = self.config["heartbeat_timeout_seconds"]
        return activity["first"] <= current["heartbeat"] + timeout and now - activity["last"] < timeout

    def busy_heartbeat_response(self, endpoint, payload):
        if endpoint != "/heartbeat" or payload.get("protocol_version") != 3:
            return None
        self.note_busy_request(endpoint, payload)
        place = self.place_name(payload["place_id"])
        session_id = payload.get("session")
        epoch = payload.get("epoch", 0)
        mode = payload.get("mode", "Edit")
        if mode != "Edit" or payload.get("snapshot_incomplete") is True:
            return None
        now = time.time()
        with self.connect() as connection:
            current = connection.execute("SELECT * FROM sessions WHERE place=? AND session=?", (place, session_id)).fetchone()
            if not current or current["epoch"] != epoch or not current["complete"]:
                return None
            expired = now - current["heartbeat"] >= self.config["heartbeat_timeout_seconds"]
            if expired and not self.busy_request_keeps_session_alive(place, current, payload, now):
                return None
            pause = self.outbound_pause(connection, place)
            restart_required = current["protocol_version"] < self.config.get("minimum_observer_protocol", 1)
            issues = [row["path"] + ": " + row["detail"] for row in connection.execute(
                "SELECT path,detail FROM autosync WHERE place=? AND state='blocked' ORDER BY path LIMIT 3", (place,))]
        return {
            "ok": True,
            "complete": True,
            "operations": [],
            "snapshot_required": False,
            "restart_required": restart_required,
            "outbound_paused": pause["paused"],
            "pause_reason": pause["reason"],
            "sync_issues": issues,
            "epoch": epoch,
        }

    def request_identity(self, endpoint, payload):
        protocol_version = payload.get("protocol_version", 1)
        request_id = payload.get("request_id")
        if request_id in (None, ""):
            if type(protocol_version) is int and protocol_version >= 3:
                raise ValueError("Observer protocol requires a request identity")
            return None
        if not isinstance(request_id, str) or len(request_id) > 100:
            raise ValueError("Invalid request identity")
        session_id = payload.get("session")
        epoch = payload.get("epoch", 0)
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 100:
            raise ValueError("Invalid observer session")
        if type(epoch) is not int or epoch < 0:
            raise ValueError("Invalid observer epoch")
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "place": self.place_name(payload["place_id"]),
            "session": session_id,
            "epoch": epoch,
            "request_id": request_id,
            "endpoint": endpoint,
            "request_hash": digest(endpoint + "\n" + serialized),
        }

    def cached_response(self, endpoint, payload):
        identity = self.request_identity(endpoint, payload)
        if not identity:
            return None
        with self.connect() as connection:
            row = connection.execute("""SELECT endpoint,request_hash,response FROM request_results
                WHERE place=? AND session=? AND epoch=? AND request_id=?""", (
                    identity["place"], identity["session"], identity["epoch"], identity["request_id"],
                )).fetchone()
        if not row:
            return None
        if row["endpoint"] != identity["endpoint"] or row["request_hash"] != identity["request_hash"]:
            raise ValueError("Request identity was reused with different content")
        response = json.loads(row["response"])
        if not isinstance(response, dict):
            raise RuntimeError("Cached workflow response is invalid")
        return response

    def set_file_base(self, connection, place, path, source, session, instance_id):
        connection.execute("INSERT OR REPLACE INTO file_bases VALUES(?,?,?)", (place, path, source))
        connection.execute("INSERT OR REPLACE INTO file_identities VALUES(?,?,?,?)",
                           (place, path, session, instance_id or ""))

    def advance_operation_base(self, connection, operation):
        base = connection.execute("SELECT source FROM file_bases WHERE place=? AND path=?",
                                  (operation["place"], operation["path"])).fetchone()
        identity = connection.execute("SELECT * FROM file_identities WHERE place=? AND path=?",
                                      (operation["place"], operation["path"])).fetchone()
        same_identity = not identity or (identity["session"] == operation["session"] and identity["instance_id"] == operation["instance_id"])
        if same_identity and base and base["source"] in (operation["baseline"], operation["source"]):
            self.set_file_base(connection, operation["place"], operation["path"], operation["source"],
                               operation["session"], operation["instance_id"])
        try:
            local = read_text(self.work_file(operation["place"], operation["path"]))
        except (OSError, UnicodeError, ValueError):
            local = None
        if local == operation["source"]:
            connection.execute("DELETE FROM leases WHERE place=? AND path=? AND created<=?",
                               (operation["place"], operation["path"], operation["created"]))

    def observe_operations(self, connection, place, record, snapshot=False):
        session = connection.execute("SELECT session,epoch,protocol_version FROM sessions WHERE place=?", (place,)).fetchone()
        if not session or record["metadata"].get("ambiguous"):
            return
        if snapshot and session["protocol_version"] >= 2:
            obsolete = connection.execute("""SELECT id FROM operations WHERE place=? AND path=? AND session<>?
                AND (state='uncertain' OR (state='applied' AND observed=0))""",
                (place, record["path"], session["session"])).fetchall()
            if obsolete and read_text(self.work_file(place, record["path"])) == record["source"]:
                for operation in obsolete:
                    connection.execute("""UPDATE operations SET state=CASE WHEN state='applied' THEN state ELSE 'superseded' END,
                        observed=1,completed_at=COALESCE(completed_at,?),result=? WHERE id=?""",
                        (time.time(), json.dumps({"reconciled_by": "new_session_matching_local_and_studio"}), operation["id"]))
        operations = connection.execute("""SELECT * FROM operations WHERE place=? AND path=? AND session=?
            AND instance_id=? AND (state IN ('delivered','uncertain','expired') OR (state='applied' AND observed=0)) ORDER BY created""",
            (place, record["path"], session["session"], record["metadata"].get("instance_id", ""))).fetchall()
        for operation in operations:
            if operation["state"] == "expired" and json.loads(operation["result"]).get("delivery") in ("not_delivered", "epoch_fenced"):
                continue
            if record["source"] == operation["source"]:
                self.advance_operation_base(connection, operation)
                result = operation["result"] if operation["state"] == "applied" else json.dumps({"confirmed_by": "source_and_identity"})
                connection.execute("UPDATE operations SET state='applied',observed=1,completed_at=COALESCE(completed_at,?),result=? WHERE id=?",
                                   (time.time(), result, operation["id"]))
            elif (snapshot and operation["state"] == "uncertain" and session["protocol_version"] >= 2
                  and session["epoch"] > operation["epoch"] and record["source"] == operation["baseline"]):
                # A v2 observer cannot execute an earlier epoch after this complete
                # inventory. The exact baseline and identity permit a new CAS attempt;
                # the abandoned operation itself must never be delivered again.
                connection.execute("UPDATE operations SET state='expired',completed_at=?,result=? WHERE id=?",
                                   (time.time(), json.dumps({"delivery": "epoch_fenced", "verified_epoch": session["epoch"]}), operation["id"]))
            elif snapshot and operation["state"] == "applied":
                # The acknowledgment fixes the base; the fresh inventory may already
                # contain a subsequent Team Create edit, which must still conflict.
                connection.execute("UPDATE operations SET observed=1 WHERE id=?", (operation["id"],))

    def event(self, connection, place: str, path: str | None, kind: str, details: dict):
        connection.execute("INSERT INTO events(place,path,kind,at,details) VALUES(?,?,?,?,?)",
                           (place, path, kind, time.time(), json.dumps(details)))

    def archive(self, connection, source: str):
        connection.execute("INSERT OR IGNORE INTO history VALUES(?,?,?)",
                           (digest(source), source, time.time()))

    def normalize(self, record: dict) -> dict:
        if not isinstance(record, dict) or not isinstance(record.get("metadata", {}), dict):
            raise ValueError("Expected a script record with object metadata")
        if len(json.dumps(record, ensure_ascii=False).encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError("Script record exceeds the 16 MB bridge limit; edit or split it in Studio")
        if not isinstance(record.get("instance_path"), str):
            raise ValueError("Expected an instance path")
        path = safe_relative(record["path"])
        if not path.endswith(".luau") or not isinstance(record["source"], str):
            raise ValueError("Expected a Luau source record")
        native = record.get("native_path")
        return {
            "path": path, "source": record["source"],
            "instance_path": record["instance_path"],
            "metadata": record.get("metadata", {}),
            "native_path": safe_relative(native) if native else None,
            "sync_status": str(record.get("sync_status", "NotSynced")),
        }

    def apply_record(self, connection, place: str, record: dict, *, snapshot=False, observed=True):
        record = self.normalize(record)
        collision = connection.execute("SELECT path FROM scripts WHERE place=? AND path=? COLLATE NOCASE", (place, record["path"])).fetchone()
        if collision and collision["path"] != record["path"]:
            raise ValueError("Case-insensitive file collision; change blocked")
        old = connection.execute("SELECT * FROM scripts WHERE place=? AND path=?",
                                 (place, record["path"])).fetchone()
        source_hash = digest(record["source"])
        self.archive(connection, record["source"])
        work_file = self.work_file(place, record["path"])
        if observed:
            self.observe_operations(connection, place, record, snapshot)
        base = connection.execute("SELECT source FROM file_bases WHERE place=? AND path=?",
                                  (place, record["path"])).fetchone()
        session = connection.execute("SELECT session FROM sessions WHERE place=?", (place,)).fetchone()
        identity = connection.execute("SELECT * FROM file_identities WHERE place=? AND path=?",
                                      (place, record["path"])).fetchone()
        local = read_text(work_file)
        held = connection.execute("SELECT 1 FROM leases WHERE place=? AND path=?", (place, record["path"])).fetchone()
        replaced_identity = bool(identity and session and identity["session"] == session["session"]
                                 and identity["instance_id"] != record["metadata"].get("instance_id", "")
                                 and (held or (base and local != base["source"])))
        if local is not None:
            self.archive(connection, local)
        if not replaced_identity and ((not held and ((base is None and local is None) or (base is not None and local == base["source"]))) or local == record["source"]):
            if local != record["source"]:
                atomic_write(work_file, record["source"], 0o644)
            self.set_file_base(connection, place, record["path"], record["source"],
                               session["session"] if session else "", record["metadata"].get("instance_id"))
        elif not replaced_identity and base and base["source"] == record["source"] and session:
            # Instance IDs are observer-session-local. An unchanged full reconnect
            # can bind the preserved exact source base to the new observer identity.
            self.set_file_base(connection, place, record["path"], base["source"],
                               session["session"], record["metadata"].get("instance_id"))
        source_file = inside(self.root / "studio" / place / "Source", record["path"])
        if not old or old["hash"] != source_hash or read_text(source_file) != record["source"]:
            connection.execute("DELETE FROM search WHERE place=? AND path=?", (place, record["path"]))
            connection.execute("INSERT INTO search VALUES(?,?,?)", (place, record["path"], record["source"]))
            atomic_write(source_file, record["source"], 0o444)
            self.event(connection, place, record["path"], "changed" if old else "added", {"hash": source_hash})
        connection.execute("INSERT OR REPLACE INTO scripts VALUES(?,?,?,?,?,?,?,?,?)", (
            place, record["path"], record["instance_path"], record["source"], source_hash,
            json.dumps(record["metadata"], sort_keys=True), record["native_path"],
            record["sync_status"], time.time(),
        ))

    def remove_record(self, connection, place, path):
        old = connection.execute("SELECT source FROM scripts WHERE place=? AND path=?", (place, path)).fetchone()
        if not old:
            return
        self.archive(connection, old["source"])
        work_file = self.work_file(place, path)
        local = read_text(work_file)
        base = connection.execute("SELECT source FROM file_bases WHERE place=? AND path=?", (place, path)).fetchone()
        held = connection.execute("SELECT 1 FROM leases WHERE place=? AND path=?", (place, path)).fetchone()
        if local is not None:
            self.archive(connection, local)
        if not held and base and local == base["source"]:
            work_file.unlink(missing_ok=True)
            connection.execute("DELETE FROM file_bases WHERE place=? AND path=?", (place, path))
            connection.execute("DELETE FROM file_identities WHERE place=? AND path=?", (place, path))
            connection.execute("DELETE FROM autosync WHERE place=? AND path=?", (place, path))
        connection.execute("DELETE FROM scripts WHERE place=? AND path=?", (place, path))
        connection.execute("DELETE FROM search WHERE place=? AND path=?", (place, path))
        snapshot = inside(self.root / "studio" / place / "Source", path)
        if os.name == "nt" and snapshot.exists():
            snapshot.chmod(stat.S_IWRITE | stat.S_IREAD)
        snapshot.unlink(missing_ok=True)
        self.event(connection, place, path, "removed", {})

    def receive(self, endpoint: str, payload: dict) -> dict:
        with self.lock:
            cached = self.cached_response(endpoint, payload)
            if cached is not None:
                return cached
            # Commit connectivity invalidation separately: a rejected stale request must
            # not roll back the fact that the previous inventory can no longer be used.
            self.observe_liveness(endpoint, payload)
            try:
                return self.receive_transaction(endpoint, payload)
            except (OSError, sqlite3.Error):
                if endpoint in ("/changes", "/record/chunk", "/snapshot/finish"):
                    place = self.place_name(payload["place_id"])
                    with self.connect() as connection:
                        current = connection.execute("SELECT session FROM sessions WHERE place=?", (place,)).fetchone()
                        if current and current["session"] == payload["session"]:
                            self.invalidate_inventory(connection, place, "Inbound update failed; full snapshot required")
                raise

    def validate_records(self, connection, place, records, deleted=(), snapshot=False):
        normalized = [self.normalize(record) for record in records]
        existing = {} if snapshot else {
            row["path"].casefold(): row["path"]
            for row in connection.execute("SELECT path FROM scripts WHERE place=?", (place,))
        }
        for record in normalized:
            path = record["path"]
            folded = path.casefold()
            if folded in existing and existing[folded] != path:
                raise ValueError("Case-insensitive file collision; batch not activated")
            existing[folded] = path
        for path in [*(record["path"] for record in normalized), *deleted]:
            self.work_file(place, path)
            inside(self.root / "studio" / place / "Source", path)
        return normalized

    def receive_transaction(self, endpoint: str, payload: dict) -> dict:
        place = self.place_name(payload["place_id"])
        request_identity = self.request_identity(endpoint, payload)
        session_id = payload["session"]
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 100:
            raise ValueError("Invalid observer session")
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT * FROM sessions WHERE place=?", (place,)).fetchone()
            mode = payload.get("mode", "Edit")
            protocol_version = payload.get("protocol_version", 1)
            epoch = payload.get("epoch", 0)
            snapshot_id = payload.get("snapshot_id", "")
            if not isinstance(snapshot_id, str) or len(snapshot_id) > 100:
                raise ValueError("Invalid snapshot identity")
            if current and current["session"] == session_id and epoch != current["epoch"]:
                raise ValueError("Stale observer epoch; start a fresh snapshot")

            def reply(body):
                now = time.time()
                if endpoint != "/disconnect":
                    connection.execute("UPDATE sessions SET heartbeat=? WHERE place=? AND session=? AND epoch=?", (
                        now, place, session_id, epoch,
                    ))
                body["epoch"] = epoch
                if endpoint.startswith("/snapshot/") or (endpoint == "/record/chunk" and payload.get("endpoint") == "/snapshot/batch"):
                    body["snapshot_id"] = snapshot_id
                if request_identity:
                    connection.execute("DELETE FROM request_results WHERE created<?", (now - 120,))
                    connection.execute("INSERT OR REPLACE INTO request_results VALUES(?,?,?,?,?,?,?,?)", (
                        request_identity["place"], request_identity["session"], request_identity["epoch"],
                        request_identity["request_id"], request_identity["endpoint"], request_identity["request_hash"],
                        json.dumps(body, ensure_ascii=False, sort_keys=True), now,
                    ))
                return body

            if type(protocol_version) is not int or protocol_version < 1:
                raise ValueError("Invalid observer protocol version")
            if mode != "Edit" and endpoint in ("/snapshot/start", "/snapshot/batch", "/snapshot/finish", "/changes", "/record/chunk"):
                raise ValueError("Source inventory requires Edit mode")
            if endpoint == "/snapshot/start":
                if current and current["session"] != session_id and time.time() - current["heartbeat"] < self.config["heartbeat_timeout_seconds"]:
                    raise ValueError("Another observer session is already active for this place")
                count = payload["count"]
                if type(count) is not int or count < 0:
                    raise ValueError("Invalid snapshot count")
                if snapshot_id and current and current["session"] == session_id and current["snapshot_id"] == snapshot_id:
                    if count != current["expected"]:
                        raise ValueError("Snapshot identity already has a different count")
                    return reply({"ok": True, "place": place, "complete": bool(current["complete"])})
                self.invalidate_inventory(connection, place, "Observer started a fresh snapshot")
                connection.execute("INSERT OR REPLACE INTO sessions(place,session,heartbeat,complete,mode,expected,last_snapshot,protocol_version,epoch,snapshot_id) VALUES(?,?,?,0,?,?,0,?,?,?)", (
                    place, session_id, time.time(), mode, count, protocol_version, epoch, snapshot_id,
                ))
                return reply({"ok": True, "place": place})
            if not current or current["session"] != session_id:
                if endpoint == "/operation/result":
                    return reply({"ok": True, "terminal": True, "snapshot_required": True})
                raise ValueError("Observer must start a new complete snapshot")
            connection.execute("UPDATE sessions SET heartbeat=?, mode=? WHERE place=?",
                               (time.time(), payload.get("mode", "Edit"), place))
            if current["expected"] < 0 and endpoint in ("/snapshot/batch", "/snapshot/finish", "/record/chunk", "/changes"):
                raise ValueError("Observer must start a new complete snapshot")
            snapshot_request = endpoint in ("/snapshot/batch", "/snapshot/finish") or (endpoint == "/record/chunk" and payload.get("endpoint") == "/snapshot/batch")
            if snapshot_request and current["snapshot_id"] != snapshot_id:
                raise ValueError("Stale snapshot identity; batch not activated")
            if snapshot_request and current["complete"]:
                return reply({"ok": True, "scripts": current["expected"], "complete": True})
            if endpoint == "/record/chunk":
                position, total = payload["position"], payload["total"]
                target = payload["endpoint"]
                text = payload["text"]
                if (type(position) is not int or type(total) is not int
                        or not 1 <= position <= total <= MAX_RECORD_CHUNKS
                        or target not in ("/snapshot/batch", "/changes")
                        or not isinstance(text, str) or len(text.encode("utf-8")) > RECORD_CHUNK_BYTES):
                    raise ValueError("Invalid chunk")
                if target == "/changes" and not current["complete"]:
                    raise ValueError("Snapshot incomplete")
                previous = connection.execute("SELECT total,endpoint FROM chunks WHERE place=? AND session=? AND transfer=?",
                                              (place, session_id, payload["transfer"])).fetchall()
                if any(item["total"] != total or item["endpoint"] != target for item in previous):
                    raise ValueError("Inconsistent chunks")
                connection.execute("INSERT OR REPLACE INTO chunks VALUES(?,?,?,?,?,?,?)",
                                   (place, session_id, payload["transfer"], position, total, target, payload["text"]))
                chunks = connection.execute("SELECT * FROM chunks WHERE place=? AND session=? AND transfer=? ORDER BY position",
                                            (place, session_id, payload["transfer"])).fetchall()
                if len(chunks) == total:
                    if any(item["total"] != total or item["endpoint"] != target for item in chunks):
                        raise ValueError("Inconsistent chunks")
                    record = self.normalize(json.loads("".join(item["text"] for item in chunks)))
                    if target == "/snapshot/batch":
                        connection.execute("INSERT OR REPLACE INTO staging VALUES(?,?,?,?)", (place, session_id, record["path"], json.dumps(record)))
                    else:
                        self.apply_record(connection, place, record)
                    connection.execute("DELETE FROM chunks WHERE place=? AND session=? AND transfer=?", (place, session_id, payload["transfer"]))
                return reply({"ok": True, "received": len(chunks)})
            if endpoint == "/snapshot/batch":
                for raw in payload["scripts"]:
                    record = self.normalize(raw)
                    connection.execute("INSERT OR REPLACE INTO staging VALUES(?,?,?,?)",
                                       (place, session_id, record["path"], json.dumps(record)))
                return reply({"ok": True})
            if endpoint == "/snapshot/finish":
                records = connection.execute("SELECT path,record FROM staging WHERE place=? AND session=?",
                                             (place, session_id)).fetchall()
                if len(records) != current["expected"]:
                    raise ValueError(f"Incomplete snapshot: {len(records)} / {current['expected']}")
                folded = [record["path"].casefold() for record in records]
                if len(set(folded)) != len(folded):
                    raise ValueError("Case-insensitive file collision; snapshot not activated")
                paths = {record["path"] for record in records}
                normalized = self.validate_records(connection, place, [json.loads(record["record"]) for record in records], snapshot=True)
                for old in connection.execute("SELECT path FROM scripts WHERE place=?", (place,)).fetchall():
                    if old["path"] not in paths:
                        self.remove_record(connection, place, old["path"])
                for record in normalized:
                    self.apply_record(connection, place, record, snapshot=True)
                connection.execute("UPDATE sessions SET complete=1,last_snapshot=? WHERE place=?", (time.time(), place))
                connection.execute("DELETE FROM staging WHERE place=?", (place,))
                self.event(connection, place, None, "snapshot", {"scripts": len(records)})
                return reply({"ok": True, "scripts": len(records)})
            if endpoint == "/changes":
                if not current["complete"]:
                    raise ValueError("Complete the initial snapshot before sending changes")
                deleted = [safe_relative(path) for path in payload.get("deleted", [])]
                records = self.validate_records(connection, place, payload.get("scripts", []), deleted)
                for record in records:
                    self.apply_record(connection, place, record)
                for path in deleted:
                    self.remove_record(connection, place, path)
                return reply({"ok": True})
            if endpoint == "/heartbeat":
                if not current["complete"] and current["expected"] < 0 and mode == "Edit" and protocol_version < 2 and "snapshot_incomplete" not in payload:
                    # Legacy observers only initiate recovery on an HTTP error.
                    raise ValueError("Observer must start a new complete snapshot")
                jobs = []
                transport_issues = []
                pause = self.outbound_pause(connection, place)
                restart_required = current["protocol_version"] < self.config.get("minimum_observer_protocol", 1)
                if current["complete"] and mode == "Edit" and not pause["paused"] and not restart_required:
                    row = connection.execute("SELECT * FROM operations WHERE place=? AND session=? AND epoch=? AND state='queued' ORDER BY created LIMIT 1",
                                             (place, session_id, current["epoch"])).fetchone()
                    if row:
                        job = dict(row)
                        large = len(json.dumps(job, ensure_ascii=False).encode()) > 600000
                        if large and payload.get("operation_chunks") is not True:
                            transport_issues.append(row["path"] + ": Reopen Studio to load the installed plugin with large-source support")
                        elif row["created"] < time.time() - OPERATION_TIMEOUT_SECONDS:
                            connection.execute("UPDATE operations SET state='expired',completed_at=?,result=? WHERE id=?",
                                               (time.time(), json.dumps({"delivery": "not_delivered", "error": "Queue deadline elapsed"}), row["id"]))
                        else:
                            if large:
                                serialized = json.dumps({"baseline": row["baseline"], "source": row["source"]}, ensure_ascii=False)
                                job = {key: row[key] for key in ("id", "path", "instance_id")}
                                job["chunks"] = (len(serialized) + OPERATION_CHUNK_CHARACTERS - 1) // OPERATION_CHUNK_CHARACTERS
                            jobs.append(job)
                            connection.execute("UPDATE operations SET state='delivered',delivered_at=?,activity_at=? WHERE id=?",
                                               (time.time(), time.time(), row["id"]))
                issues = [row["path"] + ": " + row["detail"] for row in connection.execute(
                    "SELECT path,detail FROM autosync WHERE place=? AND state='blocked' ORDER BY path LIMIT 3", (place,))]
                return reply({"ok": True, "complete": bool(current["complete"]), "operations": jobs,
                        "snapshot_required": not bool(current["complete"]), "restart_required": restart_required,
                        "outbound_paused": pause["paused"], "pause_reason": pause["reason"],
                        "sync_issues": issues + transport_issues})
            if endpoint == "/operation/chunk":
                if self.outbound_pause(connection, place)["paused"]:
                    raise ValueError("Outbound delivery is paused")
                if not current["complete"] or payload.get("mode", "Edit") != "Edit":
                    raise ValueError("Operation transfer requires a complete snapshot in Edit mode")
                if current["protocol_version"] < self.config.get("minimum_observer_protocol", 1):
                    raise ValueError("Restart Studio to load the required observer protocol")
                operation = connection.execute("SELECT baseline,source FROM operations WHERE id=? AND place=? AND session=? AND epoch=? AND state='delivered'",
                                               (payload["id"], place, session_id, current["epoch"])).fetchone()
                if not operation:
                    raise ValueError("Unknown or expired operation transfer")
                serialized = json.dumps(dict(operation), ensure_ascii=False)
                total = (len(serialized) + OPERATION_CHUNK_CHARACTERS - 1) // OPERATION_CHUNK_CHARACTERS
                position = payload["position"]
                if type(position) is not int or not 1 <= position <= total:
                    raise ValueError("Invalid operation chunk position")
                connection.execute("UPDATE operations SET activity_at=? WHERE id=?", (time.time(), payload["id"]))
                offset = (position - 1) * OPERATION_CHUNK_CHARACTERS
                return reply({"id": payload["id"], "position": position, "total": total,
                        "text": serialized[offset:offset + OPERATION_CHUNK_CHARACTERS]})
            if endpoint == "/operation/result":
                operation = connection.execute("SELECT * FROM operations WHERE id=? AND place=? AND session=?",
                                               (payload["id"], place, session_id)).fetchone()
                if not operation or operation["state"] not in ("delivered", "uncertain", "applied", "rejected", "expired"):
                    return reply({"ok": True, "terminal": True, "snapshot_required": not bool(current["complete"])})
                if operation["state"] == "expired" and (json.loads(operation["result"]).get("delivery") in ("not_delivered", "epoch_fenced") or not operation["delivered_at"]):
                    return reply({"ok": True, "terminal": True, "snapshot_required": not bool(current["complete"])})
                outcome = payload["outcome"]
                if outcome not in ("applied", "rejected"):
                    raise ValueError("Invalid operation outcome")
                if operation["state"] in ("applied", "rejected"):
                    if operation["state"] != outcome:
                        self.invalidate_inventory(connection, place, "Conflicting terminal result needs a fresh snapshot")
                        return reply({"ok": True, "terminal": True, "state": operation["state"], "snapshot_required": True})
                    return reply({"ok": True})
                details = payload.get("details", {})
                if not isinstance(details, dict):
                    raise ValueError("Operation details must be an object")
                connection.execute("UPDATE operations SET state=?,result=?,completed_at=? WHERE id=?",
                                   (outcome, json.dumps(details), time.time(), operation["id"]))
                if outcome == "applied":
                    self.advance_operation_base(connection, operation)
                    remote = connection.execute("SELECT source,metadata FROM scripts WHERE place=? AND path=?", (place, operation["path"])).fetchone()
                    if remote and remote["source"] == operation["source"] and json.loads(remote["metadata"]).get("instance_id") == operation["instance_id"]:
                        connection.execute("UPDATE operations SET observed=1 WHERE id=?", (operation["id"],))
                self.event(connection, place, operation["path"], outcome, {"operation": operation["id"]})
                return reply({"ok": True})
            if endpoint == "/disconnect":
                self.invalidate_inventory(connection, place, "Observer disconnected")
                connection.execute("UPDATE sessions SET heartbeat=0 WHERE place=?", (place,))
                return reply({"ok": True})
            raise ValueError("Unknown endpoint")

    def _status(self, connection, place: str | None = None, *, compact: bool = False) -> dict:
        result = {}
        for name in ([place] if place else self.config["places"]):
            session = connection.execute("SELECT * FROM sessions WHERE place=?", (name,)).fetchone()
            age = time.time() - session["heartbeat"] if session else None
            complete = bool(session and session["complete"])
            fresh = bool(session and age < self.config["heartbeat_timeout_seconds"])
            state = "offline" if not fresh else ("refreshing" if not complete else ("play" if session["mode"] != "Edit" else "ready"))
            restart_required = bool(session and session["protocol_version"] < self.config.get("minimum_observer_protocol", 1))
            if state == "ready" and restart_required:
                state = "restart_required"
            pause = self.outbound_pause(connection, name)
            sync_pending = connection.execute(
                "SELECT count(*) FROM autosync WHERE place=? AND state NOT IN ('synced','blocked')", (name,)).fetchone()[0]

            if compact:
                item = {"state": state}
                if state in ("offline", "refreshing", "play") and age is not None:
                    item["heartbeat_age_seconds"] = round(age, 1)
                if restart_required:
                    item["restart_required"] = True
                if pause["paused"]:
                    item["outbound_paused"] = True
                    if pause["reason"]:
                        item["pause_reason"] = pause["reason"]
                if sync_pending:
                    item["sync_pending"] = sync_pending
                blocked_count = connection.execute(
                    "SELECT count(*) FROM autosync WHERE place=? AND state='blocked'", (name,)).fetchone()[0]
                if blocked_count:
                    item["sync_issue_count"] = blocked_count
                    item["sync_issue_paths"] = [row["path"] for row in connection.execute(
                        "SELECT path FROM autosync WHERE place=? AND state='blocked' ORDER BY path LIMIT 3", (name,))]
                result[name] = item
                continue

            rows = connection.execute("SELECT metadata FROM scripts WHERE place=?", (name,)).fetchall()
            eligible = sum(bool(json.loads(row["metadata"]).get("instance_id")) and not json.loads(row["metadata"]).get("ambiguous") for row in rows)
            result[name] = {
                "state": state, "scripts": len(rows), "transport": "local-plugin",
                "editable_scripts": eligible if state == "ready" and not pause["paused"] else 0,
                "ambiguous_scripts": len(rows) - eligible,
                "heartbeat_age_seconds": round(age, 1) if age is not None else None,
                "last_full_snapshot": session["last_snapshot"] if session else None,
                "auto_sync": self.config.get("auto_sync", False),
                "outbound_paused": pause["paused"], "pause_reason": pause["reason"],
                "restart_required": restart_required,
                "protocol_version": session["protocol_version"] if session else None,
                "sync_issues": [dict(row) for row in connection.execute(
                    "SELECT path,state,detail FROM autosync WHERE place=? AND state='blocked' ORDER BY path LIMIT 20", (name,))],
                "sync_pending": sync_pending,
            }
        return result

    def status(self, place: str | None = None) -> dict:
        with self.connect() as connection:
            return self._status(connection, place)

    def query(self, query: str, place: str | None, limit: int, *, full: bool = False, observed: bool = False) -> dict:
        terms = re.findall(r"[\w]+", query)
        if not observed:
            return self.query_files(terms, place, limit, full=full)
        with self.connect() as connection:
            status = self._status(connection, place, compact=not full)
            if not terms:
                return {"status": status, "results": []}
            expression = " AND ".join('"' + term.replace('"', '""') + '"*' for term in terms)
            if full:
                rows = connection.execute("""SELECT search.place,search.path,
                    snippet(search,2,'[',']',' … ',24) AS excerpt, scripts.native_path,
                    scripts.instance_path,scripts.hash FROM search JOIN scripts
                    ON scripts.place=search.place AND scripts.path=search.path
                    WHERE search MATCH ? AND (? IS NULL OR search.place=?)
                    ORDER BY bm25(search) LIMIT ?""", (expression, place, place, max(1, min(limit, 50)))).fetchall()
            else:
                rows = connection.execute("""SELECT search.place,search.path,
                    snippet(search,2,'[',']',' … ',24) AS excerpt FROM search
                    WHERE search MATCH ? AND (? IS NULL OR search.place=?)
                    ORDER BY bm25(search) LIMIT ?""", (expression, place, place, max(1, min(limit, 50)))).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["excerpt"] = item["excerpt"][:500]
            if full:
                item["source_file"] = str(self.root / "studio" / row["place"] / "Source" / row["path"])
                item["edit_file"] = str(self.work_file(row["place"], row["path"]))
            else:
                item["source_file"] = f"studio/{row['place']}/Source/{row['path']}"
                item["edit_file"] = f"studio/{row['place']}/Files/{row['path']}"
            results.append(item)
        return {"status": status, "search_tree": "Source", "results": results}

    def query_files(self, terms, place, limit, *, full=False):
        with self.connect() as connection:
            status = self._status(connection, place, compact=not full)
            rows = connection.execute(
                "SELECT place,path,instance_path,hash,native_path FROM scripts WHERE (? IS NULL OR place=?) ORDER BY place,path",
                (place, place)).fetchall() if terms else []
        patterns = [re.compile(r"\b" + re.escape(term), re.IGNORECASE) for term in terms]
        results = []
        for row in rows:
            matches = [bool(pattern.search(row["path"])) for pattern in patterns]
            excerpt = ""
            try:
                with self.work_file(row["place"], row["path"]).open(encoding="utf-8", newline="") as stream:
                    for line in stream:
                        if not excerpt:
                            excerpt = line[:500].rstrip("\r\n")
                        for index, pattern in enumerate(patterns):
                            match = pattern.search(line)
                            if match and not matches[index]:
                                offset = max(0, match.start() - 80)
                                excerpt = line[offset:offset + 500].rstrip("\r\n")
                                matches[index] = True
                        if all(matches):
                            break
            except FileNotFoundError:
                continue
            if not all(matches):
                continue
            item = {"place": row["place"], "path": row["path"], "excerpt": excerpt}
            if full:
                item.update({key: row[key] for key in ("instance_path", "hash", "native_path")})
                item["source_file"] = str(self.root / "studio" / row["place"] / "Source" / row["path"])
                item["edit_file"] = str(self.work_file(row["place"], row["path"]))
            else:
                item["source_file"] = f"studio/{row['place']}/Source/{row['path']}"
                item["edit_file"] = f"studio/{row['place']}/Files/{row['path']}"
            results.append(item)
            if len(results) >= max(1, min(limit, 50)):
                break
        return {"status": status, "search_tree": "Files", "results": results}

    def read_source(self, place, path, start=1, lines=60, max_chars=16000):
        path = safe_relative(path)
        if place not in self.config["places"]:
            raise ValueError("Unknown place")
        if type(start) is not int or start < 1:
            raise ValueError("start must be a positive line number")
        if type(lines) is not int or not 1 <= lines <= 200:
            raise ValueError("lines must be between 1 and 200")
        if type(max_chars) is not int or not 1 <= max_chars <= 64000:
            raise ValueError("max_chars must be between 1 and 64000")
        with self.connect() as connection:
            record = connection.execute("SELECT hash FROM scripts WHERE place=? AND path=?", (place, path)).fetchone()
            if not record:
                raise ValueError("Script not found in the observed inventory")
            status = self._status(connection, place, compact=True)
        output = []
        characters = 0
        truncated = False
        next_line = None
        partial_line = None
        with self.work_file(place, path).open(encoding="utf-8", newline="") as stream:
            for number, line in enumerate(stream, 1):
                if number < start:
                    continue
                if len(output) >= lines or characters >= max_chars:
                    truncated = True
                    next_line = number
                    break
                remaining = max_chars - characters
                output.append({"line": number, "text": line[:remaining]})
                characters += min(len(line), remaining)
                if len(line) > remaining:
                    truncated = True
                    partial_line = number
                    next_line = number
                    break
        result = {"status": status, "edit_file": f"studio/{place}/Files/{path}",
                  "observed_hash": record["hash"], "lines": output, "truncated": truncated}
        if next_line is not None:
            result["next_line"] = next_line
        if partial_line is not None:
            result["partial_line"] = partial_line
        return result

    def require_ready(self, connection, place):
        session = connection.execute("SELECT * FROM sessions WHERE place=?", (place,)).fetchone()
        if not session or not session["complete"] or session["mode"] != "Edit" or time.time() - session["heartbeat"] >= self.config["heartbeat_timeout_seconds"]:
            raise WorkspaceUnavailable(TEMPORARY_READY_ERROR)
        if session["protocol_version"] < self.config.get("minimum_observer_protocol", 1):
            raise WorkspaceUnavailable("Restart Studio to load the required observer protocol")
        return session

    def checkout(self, place, path):
        path = safe_relative(path)
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.require_ready(connection, place)
            record = connection.execute("SELECT hash FROM scripts WHERE place=? AND path=?", (place, path)).fetchone()
            if not record:
                raise ValueError("Script not found")
            connection.execute("INSERT OR IGNORE INTO leases VALUES(?,?,?)", (place, path, time.time()))
        return {"file": str(self.work_file(place, path)), "studio_hash": record["hash"],
                "held": True, "note": "Automatic refresh preserves this file until apply or release"}

    def release(self, place, path):
        path = safe_relative(path)
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            local = read_text(self.work_file(place, path))
            if local is not None:
                self.archive(connection, local)
            connection.execute("DELETE FROM leases WHERE place=? AND path=?", (place, path))
            record = connection.execute("SELECT * FROM scripts WHERE place=? AND path=?", (place, path)).fetchone()
            if record:
                self.apply_record(connection, place, {**dict(record), "metadata": json.loads(record["metadata"])}, observed=False)
        return {"released": path, "local_edits_preserved": True}

    def rebase(self, place, path):
        path = safe_relative(path)
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self.require_ready(connection, place)
            base = connection.execute("SELECT source FROM file_bases WHERE place=? AND path=?", (place, path)).fetchone()
            remote = connection.execute("SELECT source,metadata FROM scripts WHERE place=? AND path=?", (place, path)).fetchone()
            local = read_text(self.work_file(place, path))
            resolution = reconcile(base["source"] if base else None, local, remote["source"] if remote else None)
            for source in (local, base["source"] if base else None, remote["source"] if remote else None):
                if source is not None:
                    self.archive(connection, source)
            if resolution["action"] == "conflict":
                output = self.state / "conflicts" / f"{place}-{time.time_ns()}.json"
                atomic_write(output, json.dumps({"place": place, "path": path, "base": base["source"] if base else None,
                                               "local": local, "studio": remote["source"] if remote else None, **resolution}, indent=2))
                return {"action": "conflict", "file": str(output), "modified": False}
            if not remote:
                return {"action": "deleted_in_studio", "modified": False}
            content = resolution["content"]
            if content is None:
                return {"action": "local_delete_requires_explicit_studio_action", "modified": False}
            connection.execute("INSERT OR IGNORE INTO leases VALUES(?,?,?)", (place,path,time.time()))
            atomic_write(self.work_file(place, path), content, 0o644)
            self.set_file_base(connection, place, path, remote["source"], session["session"], json.loads(remote["metadata"]).get("instance_id"))
            connection.execute("DELETE FROM autosync WHERE place=? AND path=?", (place, path))
            return {"action": resolution["action"], "file": str(self.work_file(place, path)), "applied_to_studio": False, "held": True}

    def queue_apply(self, place, path, automatic=False, expected_hash=None):
        path = safe_relative(path)
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.maintain_operations(connection, time.time(), place)
            if self.outbound_pause(connection, place)["paused"]:
                if automatic:
                    return {"state": "paused"}
                raise ValueError("Outbound delivery is paused")
            if automatic and not self.config.get("auto_sync", False):
                return {"state": "disabled"}
            try:
                session = self.require_ready(connection, place)
            except WorkspaceUnavailable:
                if automatic:
                    return {"state": "waiting"}
                raise
            if automatic:
                held = connection.execute("SELECT 1 FROM leases WHERE place=? AND path=?", (place, path)).fetchone()
                pending = connection.execute("SELECT 1 FROM operations WHERE place=? AND state IN ('queued','delivered')", (place,)).fetchone()
                blocked = connection.execute("SELECT 1 FROM autosync WHERE place=? AND path=? AND state='blocked'", (place, path)).fetchone()
                if held or pending or blocked:
                    return {"state": "waiting"}
            existing = connection.execute("""SELECT id FROM operations WHERE place=? AND path=? AND
                (state IN ('queued','delivered','uncertain') OR (state='applied' AND observed=0))""", (place, path)).fetchone()
            if existing:
                if automatic:
                    return {"state": "waiting"}
                raise ValueError("An operation for this file is pending or uncertain; inspect its status before retrying")
            record = connection.execute("SELECT * FROM scripts WHERE place=? AND path=?", (place, path)).fetchone()
            base = connection.execute("SELECT source FROM file_bases WHERE place=? AND path=?", (place, path)).fetchone()
            if not record or not base:
                raise ValueError("Script missing or unbased; creation/deletion uses explicit Studio operations")
            metadata = json.loads(record["metadata"])
            if metadata.get("ambiguous") or not metadata.get("instance_id"):
                raise ValueError("Ambiguous or unidentified instance; refresh or inspect in Studio")
            identity = connection.execute("SELECT * FROM file_identities WHERE place=? AND path=?", (place, path)).fetchone()
            if identity and (identity["session"] != session["session"] or identity["instance_id"] != metadata["instance_id"]):
                raise ValueError("Studio script identity changed since this local edit began; inspect and rebase")
            if base["source"] != record["source"]:
                raise ValueError("Studio changed since this local edit began; run rebase and inspect the result")
            content = read_text(self.work_file(place, path))
            if automatic and content is not None and digest(content) != expected_hash:
                return {"state": "changed"}
            if content is None:
                raise ValueError("Local file deleted; no implicit Studio deletion")
            if content == record["source"]:
                connection.execute("DELETE FROM leases WHERE place=? AND path=?", (place, path))
                return {"state": "unchanged"}
            self.normalize({"path": path, "instance_path": record["instance_path"], "source": content, "metadata": metadata})
            serialized = json.dumps({"baseline": base["source"], "source": content}, ensure_ascii=False)
            if len(serialized) > MAX_OPERATION_CHUNKS * OPERATION_CHUNK_CHARACTERS:
                raise ValueError("Operation exceeds the 1000-chunk bridge limit; edit or split it in Studio")
            operation = secrets.token_hex(16)
            self.archive(connection, content)
            self.event(connection, place, path, "queued", {"hash": digest(content), "baseline_hash": record["hash"], "operation": operation})
            connection.execute("INSERT INTO operations(id,place,session,path,instance_id,baseline,source,state,created,epoch) VALUES(?,?,?,?,?,?,?,'queued',?,?)",
                               (operation, place, session["session"], path, metadata["instance_id"], base["source"], content, time.time(), session["epoch"]))
            connection.execute("INSERT OR REPLACE INTO autosync VALUES(?,?,?,?,?,?,?)",
                               (place, path, digest(content), time.time(), "queued", "Awaiting Studio confirmation", operation))
        with self.lock:
            self.wakeup_generation += 1
        return {"id": operation, "state": "queued"}

    def resume(self, place, path):
        path = safe_relative(path)
        with self.lock, self.connect() as connection:
            self.require_ready(connection, place)
            pending = connection.execute("""SELECT 1 FROM operations WHERE place=? AND path=? AND
                (state IN ('queued','delivered','uncertain') OR (state='applied' AND observed=0))""", (place, path)).fetchone()
            if pending:
                raise ValueError("Delivery is still pending or uncertain; inspect Studio before resuming")
            connection.execute("DELETE FROM autosync WHERE place=? AND path=?", (place, path))
        return {"resumed": path, "note": "Normal conflict checks still apply; held checkouts remain paused"}

    def auto_sync(self, now=None):
        now = time.time() if now is None else now
        with self.lock, self.connect() as connection:
            self.maintain_operations(connection, now)
        if not self.config.get("auto_sync", False):
            return
        candidates = []
        with self.connect() as connection:
            paths = connection.execute("SELECT place,path FROM file_bases ORDER BY place,path").fetchall()
        for offset in range(0, len(paths), MAINTENANCE_BATCH_SIZE):
            with self.lock, self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for identity in paths[offset:offset + MAINTENANCE_BATCH_SIZE]:
                    row = connection.execute("SELECT b.place,b.path,b.source,s.source AS remote,s.metadata FROM file_bases b LEFT JOIN scripts s ON b.place=s.place AND b.path=s.path WHERE b.place=? AND b.path=?", (identity["place"], identity["path"])).fetchone()
                    if row is None:
                        continue
                    place, path = row["place"], row["path"]
                    previous = connection.execute("SELECT * FROM autosync WHERE place=? AND path=?", (place, path)).fetchone()
                    operation = connection.execute("""SELECT * FROM operations WHERE place=? AND path=? AND
                        (state IN ('queued','delivered','uncertain') OR (state='applied' AND observed=0)) ORDER BY created DESC LIMIT 1""",
                        (place, path)).fetchone()
                    if operation is None and previous and previous["operation_id"]:
                        operation = connection.execute("SELECT * FROM operations WHERE id=?", (previous["operation_id"],)).fetchone()
                    try:
                        local = read_text(self.work_file(place, path))
                    except (OSError, UnicodeError, ValueError) as error:
                        connection.execute("INSERT OR REPLACE INTO autosync VALUES(?,?,?,?,?,?,?)", (place,path,None,now,"blocked",str(error),None))
                        continue
                    observed_hash = digest(local) if local is not None else None
                    changed_at = previous["changed_at"] if previous and previous["observed_hash"] == observed_hash else now
                    state, detail = "waiting", "Waiting for a stable save and an available Studio"
                    if local is not None and local != row["source"]:
                        self.archive(connection, local)
                    base = row["source"]
                    if operation and operation["state"] == "expired" and json.loads(operation["result"]).get("delivery") in ("not_delivered", "epoch_fenced"):
                        operation = None
                    metadata = json.loads(row["metadata"]) if row["metadata"] else {}
                    session = connection.execute("SELECT * FROM sessions WHERE place=?", (place,)).fetchone()
                    file_identity = connection.execute("SELECT * FROM file_identities WHERE place=? AND path=?", (place, path)).fetchone()
                    identity_changed = bool(file_identity and session and
                        (file_identity["session"] != session["session"] or file_identity["instance_id"] != metadata.get("instance_id", "")))
                    if operation and operation["state"] == "rejected":
                        details = json.loads(operation["result"])
                        retryable = (details.get("write_attempted") is False and details.get("code") in
                                     ("play_mode", "snapshot_incomplete", "session_changed", "transfer_interrupted", "transport_interrupted", "outbound_paused"))
                        if retryable and session and session["complete"] and session["last_snapshot"] >= (operation["completed_at"] or now) and row["remote"] == operation["baseline"] and metadata.get("instance_id") == operation["instance_id"]:
                            operation = None
                    if operation and operation["state"] == "uncertain":
                        state, detail = "blocked", "Delivery is uncertain; waiting for a fresh source observation or confirmation"
                    elif operation and operation["state"] in ("queued", "delivered"):
                        state, detail = "pending", "Awaiting Studio confirmation; never resend an uncertain delivery"
                    elif local is None or row["remote"] is None:
                        state, detail = "blocked", "File or Studio script was deleted; structural changes require an explicit action"
                    elif identity_changed:
                        state, detail = "blocked", "Studio script identity changed; inspect and rebase before continuing"
                    elif operation and operation["state"] == "applied" and not operation["observed"]:
                        state, detail = "pending", "Applied; awaiting fresh Studio source observation"
                        if session and session["complete"] and now - (operation["completed_at"] or operation["created"]) > OPERATION_TIMEOUT_SECONDS:
                            self.invalidate_inventory(connection, place, "Applied result needs a fresh source inventory")
                    elif local is not None and local == row["remote"]:
                        if previous is None:
                            continue
                        state, detail = "synced", ""
                        operation = None
                    elif operation and operation["state"] in ("rejected", "expired"):
                        state, detail = "blocked", "Previous delivery was rejected or expired; inspect and rebase/resume explicitly"
                    elif connection.execute("SELECT 1 FROM leases WHERE place=? AND path=?", (place,path)).fetchone():
                        state, detail = "held", "Checkout is holding this file; apply or release when ready"
                    elif row["remote"] != base:
                        state, detail = "blocked", "Studio and local versions changed; rebase before continuing"
                    elif metadata.get("ambiguous") or not metadata.get("instance_id"):
                        state, detail = "blocked", "Ambiguous or unidentified instance; refresh or inspect in Studio"
                    elif now - changed_at >= self.config.get("sync_debounce_seconds", 1.5):
                        candidates.append((place,path,observed_hash))
                    values = (place,path,observed_hash,changed_at,state,detail,operation["id"] if operation else None)
                    if previous is None or tuple(previous) != values:
                        connection.execute("INSERT OR REPLACE INTO autosync VALUES(?,?,?,?,?,?,?)", values)
            # Keep heartbeat and checkout writers responsive during a large scan.
            time.sleep(0)
        busy_places = set()
        for place,path,observed_hash in candidates:
            if place in busy_places:
                continue
            try:
                result = self.queue_apply(place,path,automatic=True,expected_hash=observed_hash)
                if result["state"] in ("queued", "waiting", "disabled", "paused"):
                    busy_places.add(place)
            except WorkspaceUnavailable:
                continue
            except ValueError as error:
                with self.lock, self.connect() as connection:
                    connection.execute("UPDATE autosync SET state='blocked',detail=? WHERE place=? AND path=?", (str(error),place,path))

    def archive_local_changes(self, fingerprints):
        with self.connect() as connection:
            paths = connection.execute("SELECT place,path FROM file_bases ORDER BY place,path").fetchall()
        for offset in range(0, len(paths), MAINTENANCE_BATCH_SIZE):
            with self.lock, self.connect() as connection:
                for row in paths[offset:offset + MAINTENANCE_BATCH_SIZE]:
                    path = self.root / "studio" / row["place"] / "Files" / row["path"]
                    if not path.exists():
                        continue
                    info = path.stat()
                    fingerprint = (info.st_mtime_ns, info.st_size)
                    if fingerprints.get(str(path)) == fingerprint:
                        continue
                    source = read_text(self.work_file(row["place"], row["path"]))
                    if source is not None:
                        self.archive(connection, source)
                        base = connection.execute("SELECT source FROM file_bases WHERE place=? AND path=?", (row["place"], row["path"])).fetchone()
                        if base and source != base["source"]:
                            self.event(connection, row["place"], row["path"], "local_saved", {"hash": digest(source)})
                    fingerprints[str(path)] = fingerprint
            time.sleep(0)

    def write_sync_status(self):
        report = self.status()
        for entry in report.values():
            entry.pop("heartbeat_age_seconds", None)
        destination = self.root / "studio/sync-status.json"
        try:
            previous = json.loads(read_text(destination) or "{}")
        except ValueError:
            previous = {}
        last_update = previous.pop("_updated_at", 0)
        if previous != report or time.time() - last_update >= 5:
            report["_updated_at"] = time.time()
            output = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
            atomic_write(destination, output, 0o444)

    def operation(self, operation_id):
        with self.connect() as connection:
            row = connection.execute("SELECT id,place,path,state,result,delivered_at,activity_at,completed_at,observed FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not row:
                raise ValueError("Unknown operation")
            result = dict(row)
            result["result"] = json.loads(result["result"])
            return result

    def changes(self, place):
        changes = []
        with self.connect() as connection:
            for row in connection.execute("SELECT b.path,b.source,s.source AS remote FROM file_bases b LEFT JOIN scripts s ON b.place=s.place AND b.path=s.path WHERE b.place=?", (place,)):
                local = read_text(self.work_file(place, row["path"]))
                if local != row["source"]:
                    changes.append({"path": row["path"], "local_hash": digest(local) if local is not None else None,
                                    "baseline_hash": digest(row["source"]), "studio_changed": row["remote"] != row["source"]})
        return {"status": self.status(place), "changes": changes}

    def history(self, place, path):
        with self.connect() as connection:
            rows = connection.execute("SELECT kind,at,details FROM events WHERE place=? AND path=? ORDER BY id DESC LIMIT 30", (place, safe_relative(path))).fetchall()
        return {"events": [dict(row) for row in rows]}

    def export_version(self, source_hash, output):
        with self.connect() as connection:
            row = connection.execute("SELECT source FROM history WHERE hash=?", (source_hash,)).fetchone()
        if not row:
            raise ValueError("Unknown version hash")
        output = output.resolve()
        if output.exists():
            raise ValueError("Recovery output already exists; choose a new filename")
        atomic_write(output, row["source"])
        return {"exported": str(output), "hash": source_hash}

    def checkpoint(self, place):
        if place not in self.config["places"]:
            raise ValueError("Unknown place")
        with (self.state / f"checkpoint-{place}.lock").open("a+b") as lockfile:
            lock_file(lockfile)
            with self.connect() as connection:
                self.require_ready(connection, place)
                rows = connection.execute("SELECT path,source FROM scripts WHERE place=? ORDER BY path", (place,)).fetchall()
            repository = self.state / "git" / place
            repository.mkdir(parents=True, exist_ok=True)
            hooks = self.state / "git-empty-hooks"
            hooks.mkdir(exist_ok=True)
            attributes = self.state / "git-empty-attributes"
            if not attributes.exists():
                atomic_write(attributes, "", 0o600)

            def git(*arguments, check=True):
                return subprocess.run(["git", "-c", f"core.hooksPath={hooks.as_posix()}", "-c", "core.autocrlf=false",
                                       "-c", f"core.attributesFile={attributes.as_posix()}", *arguments],
                                      cwd=repository, capture_output=True, text=True, check=check, timeout=55)

            if not (repository / ".git").exists():
                git("init", "-q")
            current = {row["path"] for row in rows}
            for path in git("ls-files", "-z").stdout.split("\0"):
                if path and path not in current:
                    inside(repository, path).unlink(missing_ok=True)
            for row in rows:
                path = inside(repository, row["path"])
                if read_text(path) != row["source"]:
                    atomic_write(path, row["source"], 0o600)
            git("add", "--all", "--", ".")
            if git("diff", "--cached", "--quiet", check=False).returncode:
                git("-c", "user.name=Studio Workflow", "-c", "user.email=studio-workflow@localhost", "commit", "-q", "-m", "Studio source checkpoint")
            head = git("rev-parse", "HEAD", check=False)
            return {"repository": str(repository), "commit": head.stdout.strip() if head.returncode == 0 else None, "scripts": len(rows)}


def make_server(workspace: Workspace):
    wakeups = Wakeups(workspace)

    class Server(ThreadingHTTPServer):
        def server_close(self):
            wakeups.stopped.set()
            super().server_close()

    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

        def authenticated(self):
            return hmac.compare_digest(self.headers.get("X-Studio-Workflow-Key", ""), workspace.key)

        def do_GET(self):
            if not self.authenticated():
                self.respond(401, {"error": "Authentication required"})
                return
            if urlparse(self.path).path == "/wakeup":
                wakeups.handle(self)
                return
            if urlparse(self.path).path != "/health":
                self.respond(404, {"error": "Not found"})
                return
            self.respond(200, {"ok": True, "status": workspace.status(), "wakeup": wakeups.status()})

        def do_POST(self):
            if not self.authenticated():
                self.respond(401, {"error": "Authentication required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    self.respond(413, {"error": "Batch must be between 1 and 768 KiB"})
                    return
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Expected an object payload")
                version = payload.get("protocol_version", 1)
                if type(version) is int and version >= 2:
                    # A retry is not an acknowledgment. Old observers do not know
                    # this response, so only v2 may yield rather than wait here.
                    if not workspace.lock.acquire(timeout=0.05):
                        endpoint = urlparse(self.path).path
                        heartbeat = workspace.busy_heartbeat_response(endpoint, payload)
                        if heartbeat is not None:
                            self.respond(200, heartbeat)
                            return
                        workspace.note_busy_request(endpoint, payload)
                        self.respond(200, {"ok": True, "retry": True, "retry_after_seconds": 0.2})
                        return
                    try:
                        result = workspace.receive(urlparse(self.path).path, payload)
                    finally:
                        workspace.lock.release()
                else:
                    result = workspace.receive(urlparse(self.path).path, payload)
                self.respond(200, result)
            except (ValueError, KeyError, TypeError) as error:
                self.respond(409, {"error": str(error)})
            except Exception as error:
                self.respond(500, {"error": type(error).__name__ + ": " + str(error)})

        def log_message(self, *args):
            pass

    return Server(("127.0.0.1", workspace.config["port"]), Handler)


def serve(workspace: Workspace):
    server = make_server(workspace)
    def maintenance():
        fingerprints = {}
        last_checkpoint = 0
        while True:
            try:
                workspace.archive_local_changes(fingerprints)
                workspace.auto_sync()
                workspace.write_sync_status()
                if time.monotonic() - last_checkpoint > 300:
                    for place, status in workspace.status().items():
                        if status["state"] == "ready":
                            workspace.checkpoint(place)
                    last_checkpoint = time.monotonic()
            except Exception as error:
                print("Maintenance: " + str(error), file=sys.stderr, flush=True)
            time.sleep(1)
    threading.Thread(target=maintenance, daemon=True).start()
    print(json.dumps({"listening": server.server_address, "pid": os.getpid()}), flush=True)
    server.serve_forever()

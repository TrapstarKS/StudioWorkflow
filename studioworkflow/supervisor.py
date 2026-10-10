"""Keep one local bridge server running for a workspace."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .bridge import lock_file
from .installation import module_environment


MAX_LOG_BYTES = 5_000_000
RESTART_DELAY = 5
POLL_INTERVAL = 0.5


class RotatingLog:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def write(self, content):
        if isinstance(content, str):
            content = content.encode("utf-8", errors="replace")
        with self.lock:
            for start in range(0, len(content), MAX_LOG_BYTES):
                chunk = content[start:start + MAX_LOG_BYTES]
                if self.path.exists() and self.path.stat().st_size + len(chunk) > MAX_LOG_BYTES:
                    self.path.replace(self.path.with_name(self.path.stem + ".previous.log"))
                with self.path.open("ab") as stream:
                    stream.write(chunk)


def terminate(child, *, force=False):
    try:
        if os.name == "nt":
            if force:
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                if child.poll() is None:
                    child.kill()
            else:
                try:
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                except (AttributeError, OSError):
                    child.terminate()
        else:
            os.killpg(child.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


def _drain(stream, log):
    try:
        read = getattr(stream, "read1", stream.read)
        while True:
            content = read(8192)
            if not content:
                return
            log.write(content)
    finally:
        stream.close()


def run(workspace):
    workspace.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop_path = workspace.state / "supervisor.stop"
    lock = (workspace.state / "supervisor.lock").open("a+b")
    try:
        lock_file(lock, blocking=False)
    except BlockingIOError:
        lock.close()
        return False
    except BaseException:
        lock.close()
        raise
    stop_path.unlink(missing_ok=True)
    supervisor_log = RotatingLog(workspace.state / "supervisor.log")
    server_log = RotatingLog(workspace.state / "serve.log")
    stopped = threading.Event()
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.signal(signum, lambda signum, frame: stopped.set())
    child = None
    reader = None
    retry_at = 0
    try:
        while not stopped.is_set() and not stop_path.exists():
            if child is not None and child.poll() is not None:
                supervisor_log.write(f"Bridge exited with code {child.returncode}; restarting in {RESTART_DELAY} seconds.\n")
                terminate(child)
                if reader is not None:
                    reader.join(timeout=2)
                child = None
                reader = None
                retry_at = time.monotonic() + RESTART_DELAY
            if child is None and time.monotonic() >= retry_at:
                command = [sys.executable, "-m", "studioworkflow", "--root", str(workspace.root), "serve"]
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                try:
                    child = subprocess.Popen(command, cwd=workspace.root, env=module_environment(),
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             start_new_session=os.name != "nt", creationflags=creation_flags)
                except OSError as error:
                    supervisor_log.write(f"Could not start bridge: {error}; retrying in {RESTART_DELAY} seconds.\n")
                    retry_at = time.monotonic() + RESTART_DELAY
                else:
                    supervisor_log.write(f"Started bridge process {child.pid}.\n")
                    reader = threading.Thread(target=_drain, args=(child.stdout, server_log), daemon=True)
                    reader.start()
            stopped.wait(POLL_INTERVAL)
    finally:
        try:
            if child is not None:
                terminate(child)
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    terminate(child, force=True)
                    child.wait(timeout=8)
            if reader is not None:
                reader.join(timeout=2)
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
            stop_path.unlink(missing_ok=True)
            lock.close()
    return True

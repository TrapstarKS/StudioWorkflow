"""HTTP-only wakeup fallback while the optional WebSocket transport is unavailable."""

from __future__ import annotations

import threading


class Wakeups:
    """Keep the core workflow available through its authenticated HTTP API."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.enabled = False
        self.stopped = threading.Event()

    def status(self) -> dict:
        return {"available": False, "connections": 0}

    def handle(self, handler) -> None:
        handler.respond(503, {
            "error": "WebSocket wakeups unavailable; use HTTP polling",
        })

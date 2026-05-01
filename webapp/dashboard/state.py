"""
Shared mutable state for the sender and receiver background threads.

Each state object holds a reference to the running thread, its stop event,
and the latest stats/spectrum dicts to serve over WebSocket.

All mutations go through a Lock. Reads of the dicts are safe because
the dict reference is replaced atomically (Python GIL ensures this for
simple attribute assignment, but we use the lock anyway for clarity).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _StreamState:
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    running: bool = False
    stats: dict[str, Any] = field(default_factory=dict)
    spectrum: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self, target, kwargs: dict) -> None:
        with self._lock:
            if self.running:
                return
            self.stop_event.clear()
            self.stats = {}
            self.spectrum = {}
            self.thread = threading.Thread(
                target=target, kwargs=kwargs, daemon=True
            )
            self.running = True
            self.thread.start()

    def stop(self) -> None:
        with self._lock:
            self.stop_event.set()
            self.running = False

    def update_stats(self, stats: dict) -> None:
        with self._lock:
            self.stats = stats

    def update_spectrum(self, spectrum: dict) -> None:
        with self._lock:
            self.spectrum = spectrum


sender_state = _StreamState()
receiver_state = _StreamState()

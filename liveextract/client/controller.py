"""
Thread-safe execution controller for pausing, resuming, and cancelling stream extraction.
"""

import threading
import time
from typing import Callable, List


class StreamController:
    """Controls live streaming extraction with real-time pause, resume, and cancel."""

    def __init__(self):
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._cancel_event = threading.Event()
        self._state_lock = threading.Lock()
        self._status: str = "idle"
        self._status_listeners: List[Callable[[str], None]] = []

    @property
    def status(self) -> str:
        with self._state_lock:
            return self._status

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def set_status(self, new_status: str) -> None:
        with self._state_lock:
            self._status = new_status
            listeners = list(self._status_listeners)
        for cb in listeners:
            try:
                cb(new_status)
            except Exception:
                pass

    def add_status_listener(self, cb: Callable[[str], None]) -> None:
        with self._state_lock:
            self._status_listeners.append(cb)

    def start(self) -> None:
        self._cancel_event.clear()
        self._pause_event.set()
        self.set_status("running")

    def pause(self) -> None:
        if self.is_cancelled:
            return
        self._pause_event.clear()
        self.set_status("paused")

    def resume(self) -> None:
        if self.is_cancelled:
            return
        self._pause_event.set()
        self.set_status("running")

    def toggle_pause(self) -> bool:
        if self.is_paused:
            self.resume()
            return True
        else:
            self.pause()
            return False

    def cancel(self) -> None:
        self._cancel_event.set()
        self._pause_event.set()
        self.set_status("cancelled")

    def reset(self) -> None:
        self._cancel_event.clear()
        self._pause_event.set()
        self.set_status("idle")

    def check_and_wait(self, poll_interval: float = 0.1) -> bool:
        if self._cancel_event.is_set():
            return False

        while not self._pause_event.is_set():
            if self._cancel_event.is_set():
                return False
            time.sleep(poll_interval)

        return not self._cancel_event.is_set()

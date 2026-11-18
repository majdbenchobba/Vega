"""Keep independent service workers alive without replaying uncertain work."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import traceback


class WorkerSupervisor:
    def __init__(self, directory, stop, recover, *, retry_seconds=2):
        self.directory = Path(directory)
        self.stop, self.recover = stop, recover
        self.retry_seconds = retry_seconds
        self.lock = threading.RLock()
        self.states, self.threads = {}, {}

    def snapshot(self):
        with self.lock:
            return {
                name: {**state, "alive": self.threads[name].is_alive()}
                for name, state in self.states.items()
            }

    def record_failure(self, name, error):
        with self.lock:
            state = self.states[name]
            state.update({
                "state": "recovering", "last_failure_at": time.time(),
                "failures": state["failures"] + 1,
                "error": getattr(error, "sqlite_errorname", type(error).__name__),
                # Function names and line locations only. No locals, exception
                # bodies, request URLs, captions, credentials or message text.
                "frames": [
                    {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                    for frame in traceback.extract_tb(error.__traceback__)[-8:]
                ],
            })
            try:
                temporary = self.directory / "worker-health.json.tmp"
                temporary.write_text(json.dumps(self.states, indent=2), encoding="utf-8")
                temporary.replace(self.directory / "worker-health.json")
            except OSError:
                pass  # Health remains visible from the in-memory snapshot.

    def run(self, name, target, arguments):
        recovery_needed = False
        while not self.stop.is_set():
            try:
                if recovery_needed:
                    self.recover(name)
                    recovery_needed = False
                with self.lock:
                    self.states[name].update({"state": "running", "started_at": time.time()})
                target(*arguments)
                if not self.stop.is_set():
                    raise RuntimeError("A service worker returned unexpectedly.")
            except Exception as error:
                self.record_failure(name, error)
                recovery_needed = True
                failures = self.states[name]["failures"]
                self.stop.wait(min(30, self.retry_seconds * min(8, failures)))
        with self.lock:
            self.states[name]["state"] = "stopped"

    def start(self, name, target, arguments=(), *, daemon=True):
        with self.lock:
            if name in self.threads and self.threads[name].is_alive():
                return self.threads[name]
            self.states[name] = {"state": "starting", "failures": 0}
            thread = threading.Thread(target=self.run, args=(name, target, arguments), name="Vega-" + name, daemon=daemon)
            self.threads[name] = thread
            thread.start()
            return thread

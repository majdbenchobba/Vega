"""Loopback listeners that cannot share a Windows port with another service."""
import socket
import contextlib
import threading
import time
from http.server import ThreadingHTTPServer


class ReadTimings:
    """Bounded process-local timings: fixed labels, no bodies, paths or secrets."""
    def __init__(self, labels, *, clock=time.perf_counter):
        self.clock = clock
        self.started = clock()
        self.lock = threading.Lock()
        self.operations = {
            label: {"count": 0, "failures": 0, "slow": 0, "total_ms": 0., "last_ms": 0.,
                    "max_ms": 0., "active": 0, "pending": {}}
            for label in labels
        }

    @contextlib.contextmanager
    def measure(self, label):
        start = self.clock()
        token = object()
        with self.lock:
            item = self.operations[label]
            item["active"] += 1
            if len(item["pending"]) < 128:
                item["pending"][token] = start
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            duration = max(0., self.clock() - start) * 1000
            with self.lock:
                item["active"] -= 1
                item["pending"].pop(token, None)
                item["count"] += 1
                item["failures"] += int(failed)
                item["slow"] += int(duration >= 1000)
                item["last_ms"] = duration
                item["max_ms"] = max(duration, item["max_ms"])
                item["total_ms"] += duration

    def call(self, label, operation, *args, **kwargs):
        with self.measure(label):
            return operation(*args, **kwargs)

    def snapshot(self):
        now = self.clock()
        with self.lock:
            operations = {
                name: {
                    **{key: item[key] for key in ("count", "failures", "slow", "active")},
                    "last_ms": round(item["last_ms"], 2),
                    "max_ms": round(item["max_ms"], 2),
                    "mean_ms": round(item["total_ms"] / item["count"], 2) if item["count"] else 0.,
                    "untracked_active": item["active"] - len(item["pending"]),
                    # A bounded sample cannot establish the oldest active age
                    # while any operation's start is missing.
                    "oldest_active_ms": None if item["active"] > len(item["pending"]) else
                    (round(max(0., now - min(item["pending"].values())) * 1000, 2)
                     if item["pending"] else 0.),
                }
                for name, item in self.operations.items() if item["count"] or item["active"]
            }
        return {"version": 2, "uptime_seconds": round(max(0., now - self.started), 2), "operations": operations}


class ExclusiveLoopbackServer(ThreadingHTTPServer):
    allow_reuse_address = False
    # A page opens more connections than the stdlib's five pending slots.
    # Keep asset/API bursts queued while the accept thread is briefly busy.
    request_queue_size = 64

    def server_bind(self):
        if self.server_address[0] not in ("127.0.0.1", "::1"):
            raise ValueError("Vega HTTP services must bind to loopback.")
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

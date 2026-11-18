"""Bounded owner-requested incident observation inside Vega's existing watchdog."""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
import uuid

from notification_privacy import redact
from memory_policy import contains_secret

LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
DEFAULT_REPORTS_ROOT = LOCAL_APP_DATA / "Vega" / "reports"
SELF = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://vega.local/owner-reports"))
UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
SEVERITIES = {"critical", "error", "warning", "action_needed", "action_required", "recovery", "recovered", "resolved"}
RECOVERY = {"recovery", "recovered", "resolved"}
MAX_RECORD = 64 * 1024


def timestamp(value):
    if type(value) in (int, float) and math.isfinite(value):
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo:
            return parsed.timestamp()
    raise ValueError("Use a dated project report.")


def concise(value, maximum):
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        value = "; ".join(value[:3])
    if not isinstance(value, str) or not value.strip() or contains_secret(value):
        raise ValueError("The incident needs concise, non-secret evidence.")
    value = " ".join(redact(value).split())
    return value if len(value) <= maximum else value[:maximum - 1].rstrip() + "…"


class AwayMonitor:
    DEFAULT_REPORTS_ROOT = DEFAULT_REPORTS_ROOT

    def __init__(self, store, milestones, *, reports_root=DEFAULT_REPORTS_ROOT, clock=time.time, disk_free=None):
        self.store, self.milestones, self.clock = store, milestones, clock
        self.root = Path(reports_root).absolute()
        self.disk_free = disk_free or (lambda: shutil.disk_usage(self.root.anchor).free)
        self.last_tick = 0

    @staticmethod
    def event_key(reference, reporter, identity, phase="incident"):
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()[:20]
        return "away:" + digest(reference) + ":" + digest(reporter + ":" + identity) + ":" + phase

    def read(self, reporter):
        if not UUID.fullmatch(reporter):
            raise ValueError("Unknown report identity.")
        path = self.root / (reporter + ".json")
        for part in (*reversed(path.parents), path):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Report links are not accepted.")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_RECORD:
            raise ValueError("Use a small regular report file.")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(MAX_RECORD + 1)
            after = os.fstat(stream.fileno())
        final = path.lstat()
        if (len(raw) > MAX_RECORD or (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) !=
                (opened.st_dev, opened.st_ino, after.st_mtime_ns, after.st_size)
                or (final.st_dev, final.st_ino, final.st_mtime_ns, final.st_size) !=
                (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)):
            raise ValueError("The report changed during inspection.")
        value = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("Use a project record.")
        return value

    def receipt(self, reference, reporter, identity, phase="incident"):
        key = "manual:" + self.event_key(reference, reporter, identity, phase)
        with self.store.db() as db:
            row = db.execute("SELECT event_id,state,message_id,sent_at,error FROM outbox WHERE event_id=?",
                             (key,)).fetchone()
        return dict(row) if row else None

    def admit(self, window, reporter, source, alert, *, observed_at=None):
        if not isinstance(alert, dict):
            raise ValueError("Use a structured incident.")
        severity = str(alert.get("severity", "")).lower().replace("-", "_")
        identity = alert.get("event_id", alert.get("id"))
        if (severity not in SEVERITIES or not isinstance(identity, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", identity)):
            raise ValueError("Use a stable incident ID and severity.")
        recovery = severity in RECOVERY
        if recovery:
            original = alert.get("recovery_of", identity)
            if not isinstance(original, str):
                raise ValueError("A recovery needs its original incident.")
            prior = self.receipt(window["request_ref"], reporter, original)
            if not prior or prior["state"] != "sent":
                return {"ignored": "No sent incident to recover."}
            identity = original
        evidence = concise(alert.get("evidence"), 320)
        action = alert.get("action_needed", alert.get("action"))
        label = "Recovery" if recovery else "Action needed" if severity.startswith("action_") else "Incident"
        observed = datetime.fromtimestamp(observed_at or self.clock(), timezone.utc).strftime("%H:%M UTC")
        text = f"{label} · {concise(source['title'], 70)}\nReported {observed}: {evidence}"
        if action:
            text += "\nAction: " + concise(action, 220)
        key = self.event_key(window["request_ref"], reporter, identity, "recovery" if recovery else "incident")
        return self.milestones.requested_text(source["task"], key, text, window["request_ref"])

    def _receipts(self, reference):
        with self.store.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT o.event_id,o.state,o.message_id,o.sent_at,o.error FROM outbox o "
                "JOIN requested_text_notices n ON n.event_id=o.event_id "
                "WHERE n.request_ref=? ORDER BY o.created_at", (reference,))]

    def _expire(self, reference):
        with self.store.db() as db:
            key = "requested_window:" + reference
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row:
                guard = json.loads(row["value"])
                guard["enabled"] = False
                db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(guard), key))
            db.execute("UPDATE outbox SET state='suppressed',error=? WHERE state='queued' AND "
                       "event_id IN (SELECT event_id FROM requested_text_notices WHERE request_ref=?)",
                       ("The owner-away notification window ended or was revoked.", reference))

    def _health(self, window, workers, previous):
        now = self.clock()
        bad = {}
        for name in ("telegram-poll", "delivery", "task-scan", "conversation", "n8n"):
            item = workers.get(name)
            if item and (not item.get("alive") or item.get("state") != "running"):
                bad["worker-" + name] = f"Vega's {name} worker is unavailable."
        poll = self.store.get("last_poll_at")
        if type(poll) in (int, float) and now - poll > 60:
            bad["telegram"] = "Vega has not received a successful Telegram polling response for over a minute."
        free = self.disk_free()
        if free < 1024 ** 3:
            bad["disk"] = "The laptop has less than 1 GB of free disk space."
        old = previous.get("health_issues", {})
        current = {}
        source = {"task": "vega", "title": "Vega service"}
        for name, evidence in bad.items():
            issue = old.get(name) or {"since": now, "id": name + "-" + str(int(now))}
            current[name] = issue
            if now - issue["since"] >= 60:
                self.admit(window, SELF, source, {"event_id": issue["id"], "severity": "error", "evidence": evidence})
        for name, issue in old.items():
            if name not in bad:
                self.admit(window, SELF, source, {"event_id": issue["id"], "severity": "recovery",
                           "evidence": "The previously reported " + name + " condition has cleared in Vega's current health checks."})
        return current, free

    def _save(self, status):
        self.store.set("away_session_monitor", status)
        report = {
            "title": "V Main", "updated_at_utc": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
            "goal": "Complete Vega readiness while preserving owner data; voice validation is deferred.",
            "active_job": "Bounded incident checks in Vega's existing watchdog.",
            "verified_work": ["Project activity/map/work-panel verification deployed; 122 targeted checks passed.",
                              "26 registered projects; 15 connected chats; 11 without a verified replacement."],
            "checks": {"voice": "deferred", "project_verification": "passed"},
            "deploy_backup_state": "Original profile, settings, history and paused/muted preferences preserved.",
            "incident_monitor": status,
            "next_action": "Observe incidents until the cutoff; continue authorised non-voice work.",
            "blockers": ["Voice recognition and real microphone timing await the owner's later tests."],
            "alert": None,
        }
        if any(item["state"] in {"failed", "uncertain", "suppressed"} for item in status.get("receipts", [])):
            report["blockers"].append("At least one incident has no confirmed Telegram delivery; see its receipt.")
        destination = self.root / (SELF + ".json")
        # This is this chat's own compact record, never another project's report.
        for part in (*reversed(self.root.parents), self.root):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Report output cannot traverse a link.")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root,
                                         prefix="vega-report-", suffix=".tmp", delete=False) as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            temporary = Path(stream.name)
        temporary.replace(destination)

    def tick(self, workers=None):
        now = self.clock()
        if now - self.last_tick < 15:
            return
        self.last_tick = now
        window = self.store.get("away_session_window")
        if not isinstance(window, dict):
            return
        reference = window.get("request_ref", "")
        if not re.fullmatch(r"codex:[a-f0-9-]{36}:[a-f0-9-]{36}", reference):
            raise ValueError("The reviewed owner request is unavailable.")
        guard = self.store.get("requested_window:" + reference)
        if not isinstance(guard, dict):
            raise ValueError("The bounded delivery grant is unavailable.")
        if (window.get("enabled") is not True or guard.get("enabled") is not True
                or now >= guard.get("end_at", 0)):
            self._expire(reference)
            previous = self.store.get("away_session_monitor", {})
            if previous.get("state") != "ended":
                self._save({"state": "ended", "checked_at": now, "end_at": guard.get("end_at"),
                            "receipts": self._receipts(reference)})
            return
        with self.store.db() as db:
            self.milestones._owner(db, window.get("owner"))
            self.milestones._requested_source(db, window["owner"], reference)
        sources = window.get("reporters")
        if not isinstance(sources, dict) or len(sources) > 50:
            raise ValueError("The reviewed report sources are unavailable.")
        seen, unavailable = 0, []
        for reporter, source in sources.items():
            if reporter == SELF:
                continue
            try:
                record = self.read(reporter)
                stamp = next((record[key] for key in
                              ("updated_at_utc", "timestampUtc", "timestamp", "timestamp_utc",
                               "updated_at", "updated_at_tunis", "recorded_utc")
                              if record.get(key) is not None), None)
                stamp = timestamp(stamp)
                if not guard["start_at"] - 120 <= stamp <= now + 120:
                    raise ValueError("The report is not current.")
                seen += 1
                if record.get("alert") is not None:
                    self.admit(window, reporter, source, record["alert"], observed_at=stamp)
            except (OSError, ValueError, KeyError, TypeError):
                unavailable.append(reporter)
        previous = self.store.get("away_session_monitor", {})
        issues, free = self._health(window, workers or {}, previous)
        self._save({"state": "active", "checked_at": now, "end_at": guard["end_at"],
                    "reports_seen": seen, "unavailable_reports": unavailable,
                    "health_issues": issues, "free_disk_bytes": free, "receipts": self._receipts(reference),
                    "delivery": "Existing Vega owner-requested queue; no extra bot or Telegram poller."})

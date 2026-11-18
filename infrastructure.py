"""Private, read-only infrastructure view with explicit observation timestamps."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import threading
import time

from infrastructure_catalog import catalog, AUDITED_AT
from infrastructure_probe import collect, save

ROOT = Path(__file__).resolve().parent


def seconds(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def join_inventory(base, inventory):
    """Keep running state, backup presence and tested recovery as separate facts."""
    result = deepcopy(base)
    scopes = [({**s["last_success"], "stale": True, "last_attempt_at": s.get("checked_at")}
               if not s.get("ok") and s.get("last_success") else s) for s in inventory.get("scopes", [])]
    machines = []
    volumes = {row["id"]: row for scope in scopes if scope.get("ok") and scope["service"] == "volumes"
               for row in scope["rows"]}
    snapshots = [row for scope in scopes if scope.get("ok") and scope["service"] == "snapshots" for row in scope["rows"]]
    health_scopes = {s.get("region"): s for s in scopes if s["service"] == "instance-status"}
    for scope in scopes:
        if not scope.get("ok") or scope["service"] not in ("ec2", "lightsail"):
            continue
        for item in scope["rows"]:
            key = scope["service"] + ":" + item["id"]
            facts = [
                {"label": "Resource", "value": item["id"]},
                {"label": "Region", "value": scope["region"]},
                {"label": "Type", "value": item.get("type") or "Unknown"},
                {"label": "Observed state", "value": item.get("state") or "Unknown"},
                {"label": "Operating system", "value": item.get("platform") or "Unknown"},
            ]
            role = base.get("resource_roles", {}).get(item["id"])
            if role:
                facts.append({"label": "Role", "value": role})
            for disk in item.get("disks", []):
                detail = volumes.get(disk.get("id"), disk)
                size = detail.get("size_gb")
                facts.append({"label": "Disk", "value": f"{size} GiB · {detail.get('type', 'system disk')}" if size else "Size not verified"})
                if "encrypted" in detail:
                    facts.append({"label": "Disk encryption", "value": "On" if detail["encrypted"] else "Off"})
                if "delete_on_termination" in disk:
                    facts.append({"label": "On termination", "value": "This disk is set to delete" if disk["delete_on_termination"] else "This disk is set to remain"})
                copies = [s for s in snapshots if s.get("volume") == disk.get("id")]
                if disk.get("id"):
                    checked = any(s.get("ok") and s["service"] == "snapshots" and s["region"] == scope["region"] for s in scopes)
                    facts.append({"label": "Owned EBS snapshots", "value": str(len(copies)) + " found · same AWS account" if checked else "Not verified"})
                    if copies:
                        facts.append({"label": "Newest EBS snapshot", "value": max(s["started"] for s in copies)})
            facts += [{"label": "Stop", "value": "Compute stops. Persistent EBS remains; instance-store/RAM data is not protected."},
                      {"label": "AWS suspension", "value": "Do not rely on same-account disk/snapshots for independent access."},
                      {"label": "Application health", "value": "Instance state is not an application health test."}]
            value = dict(id=key, label=item.get("name") or item["id"], kind="runtime", provider="aws",
                         summary=f"{scope['service'].upper()} · {item.get('type', '')} · {scope['region']}",
                         observed_state=item.get("state"), observed_at=scope["checked_at"], facts=facts,
                         observed_stale=scope.get("stale", False),
                         sources=[], machine=True, region=scope["region"], machine_type=item.get("type"))
            if scope["service"] == "ec2" and scope["region"] in health_scopes:
                health_scope = health_scopes[scope["region"]]
                status = next((row for row in health_scope["rows"] if row["id"] == item["id"]), {})
                value["health"] = {key: status[key] for key in ("system", "instance", "ebs") if status.get(key)}
                value["health"].update(checked_at=health_scope.get("checked_at"),
                                       stale=health_scope.get("stale", False),
                                       available=bool(health_scope.get("ok") and status and not health_scope.get("stale")))
            result["nodes"].append(value)
            machines.append(value)
    known = {n["id"] for n in result["nodes"]}
    # Missing cloud rows are unknown, never silently removed from the dependency graph.
    for edge in result["edges"]:
        if edge["target"] not in known:
            result["nodes"].append(dict(id=edge["target"], label=edge["target"], kind="runtime", provider="aws",
                                        summary="Current inventory unavailable", facts=[], sources=[], observed_state="unknown",
                                        machine=edge["target"].startswith(("ec2:", "lightsail:"))))
            known.add(edge["target"])
    r2 = next((s for s in scopes if s["id"] == "cloudflare:r2"), {})
    names = {row["name"] for row in r2.get("rows", [])}
    complete = r2.get("ok") and r2.get("complete", False)
    for node in result["nodes"]:
        if node.get("bucket"):
            node["observed_state"] = ("present" if node["bucket"] in names else "absent") if complete else "unknown"
            node["observed_stale"] = r2.get("stale", False)
            node["observed_at"] = r2.get("checked_at")
            node["facts"].append({"label": "Bucket", "value": node["bucket"]})
            node["facts"].append({"label": "Meaning", "value": "A listed bucket alone does not prove backup contents or recovery."})
    for backup in result["backups"]:
        bucket = backup.get("bucket")
        backup["presence"] = ("present" if bucket in names else "absent") if complete and bucket and not r2.get("stale") else "unknown"
        backup["last_presence"] = ("present" if bucket in names else "absent") if complete and bucket else "unknown"
        if bucket and backup["presence"] == "absent":
            backup["copy"] = "Destination absent"
        backup["presence_checked_at"] = r2.get("checked_at")
    result["machines"] = machines
    result["cloud"] = inventory
    return result


class InfrastructureMap:
    def __init__(self, vega, *, path=ROOT / ".state/infrastructure-current.json", collector=collect, clock=time.time):
        self.vega, self.path, self.collector, self.clock = vega, Path(path), collector, clock
        self.lock = threading.RLock()
        self.active, self.last_start, self.refresh_error = False, -float("inf"), None

    def _principal(self, owner):
        return self.vega.desktop_workspace._principal(owner)

    def refresh_cloud(self, owner, payload):
        if payload:
            raise ValueError("Cloud inventory refresh takes no action or path fields.")
        principal = self._principal(owner)
        if shutil.disk_usage(ROOT).free < 10 * 1024**3 + 10_000_000:
            raise ValueError("Inventory refresh needs the existing disk reserve.")
        with self.lock:
            if self.active:
                return {"refreshing": True}
            if self.clock() - self.last_start < 120:
                return {"refreshing": False, "message": "Cloud inventory was checked recently."}
            self.active, self.last_start, self.refresh_error = True, self.clock(), None

        def run():
            try:
                if self._principal(owner) != principal:
                    return
                value = self.collector()
                # Only metadata generated by this collector is saved. Never
                # read paths, credentials or operation names from HTTP input.
                with self.lock:
                    if self._principal(owner) == principal:
                        save(value, self.path)
            except Exception:
                self.refresh_error = "Cloud inventory could not finish. Previous observations are retained."
            finally:
                self.active = False
        threading.Thread(target=run, name="vega-infrastructure-read", daemon=True).start()
        return {"refreshing": True}

    def snapshot(self, owner):
        principal = self._principal(owner)
        base = catalog()
        try:
            if self.path.stat().st_size > 2_000_000:
                raise ValueError("Invalid snapshot")
            cloud = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(cloud, dict) or cloud.get("version") != 1
                    or not isinstance(cloud.get("scopes"), list)
                    or any(not isinstance(s, dict) or not {"id", "service", "rows"} <= s.keys()
                           or not isinstance(s["rows"], list) for s in cloud["scopes"])):
                raise ValueError("Invalid snapshot")
        except (OSError, ValueError):
            cloud = {"version": 1, "scopes": [], "errors": [{"provider": "Cloud", "error": "No current inventory"}]}
        result = join_inventory(base, cloud)
        try:
            workspace = self.vega.desktop_workspace.snapshot(owner)
            current = {p["id"]: p for p in workspace["projects"]}
            mapped = {g["project_id"] for g in result["groups"]}
            for project in workspace["projects"]:
                if project["id"] not in mapped:
                    group = "new:" + project["id"]
                    result["groups"].append({"id": group, "label": project["label"], "project_id": project["id"]})
                    result["nodes"].append(dict(id=group, label=project["label"], kind="project", provider="other",
                                                summary="Infrastructure not mapped yet", facts=[], sources=[], group=group,
                                                project_id=project["id"], unmapped=True))
            for group in result["groups"]:
                group["current"] = group["project_id"] in current
                if group["current"]:
                    group["label"] = current[group["project_id"]]["label"]
                chats = [c for c in workspace["chats"] if c.get("project_id") == group["project_id"]]
                group["chats"], group["running"] = len(chats), sum(c["running"] for c in chats)
            result["workspace"] = {"available": not workspace.get("unavailable", False),
                                   "snapshot_at": workspace["snapshot_at"], "limited": workspace.get("limited", False),
                                   "projects": len(current), "chats": len(workspace["chats"]),
                                   "independent": sum(c.get("project_id") is None for c in workspace["chats"])}
        except PermissionError:
            raise
        except Exception:
            result["workspace"] = {"available": False, "error": "Codex inventory unavailable"}
        audited = seconds(AUDITED_AT)
        for source in result["sources"].values():
            path = Path(source.pop("path"))
            source["file"] = path.name
            try:
                stat = path.stat()
                source["changed"] = stat.st_mtime > audited
                source["available"] = True
                source["modified_at"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            except OSError:
                source["available"], source["changed"] = False, None
        for backup in result["backups"]:
            backup["source_changed"] = any(result["sources"][s].get("changed") for s in backup["sources"])
        result["refresh"] = {"active": self.active, "error": self.refresh_error}
        result["observed_at"] = self.clock()
        result["local"] = {"host": "Connected", "free_gib": round(shutil.disk_usage(ROOT).free / 1024**3, 1)}
        laptop = next((n for n in result["nodes"] if n["id"] == "laptop"), None)
        if laptop is not None:
            laptop.update(observed_state="connected", observed_at=result["observed_at"])
        if self._principal(owner) != principal:
            raise PermissionError("The owner context changed.")
        return result

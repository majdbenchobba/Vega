"""Bounded, read-only cloud metadata inventory. No login, uploads or changes."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parent


def _cloudflare_account():
    account = os.environ.get("VEGA_CLOUDFLARE_ACCOUNT_ID", "")
    if not re.fullmatch(r"[a-f0-9]{32}", account):
        raise ValueError("Configure a Cloudflare account ID before requesting its inventory.")
    return account


def _cloudflare_auth_path():
    configured = os.environ.get("VEGA_WRANGLER_AUTH_FILE")
    if configured:
        return Path(configured).expanduser()
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return appdata / "xdg.config" / ".wrangler" / "config" / "default.toml"


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, limit=120):
    return re.sub(r"[\x00-\x1f\x7f]", "", str(value or ""))[:limit]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("Redirect refused")


def aws(service, operation, region, query):
    allowed = {("sts", "get-caller-identity"), ("ec2", "describe-regions"),
               ("ec2", "describe-instances"), ("lightsail", "get-regions"),
               ("lightsail", "get-instances"), ("s3api", "list-buckets"),
               ("ec2", "describe-volumes"), ("ec2", "describe-snapshots"),
               ("ec2", "describe-instance-status")}
    if (service, operation) not in allowed or not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", region):
        raise ValueError("Unsupported inventory operation")
    aws_cli = os.environ.get("VEGA_AWS_CLI") or shutil.which("aws") or "aws"
    profile = os.environ.get("VEGA_AWS_PROFILE", "default")
    command = [aws_cli, service, operation, "--profile", profile, "--region", region,
         "--query", query, "--output", "json", "--no-cli-pager",
         "--cli-connect-timeout", "5", "--cli-read-timeout", "8"]
    if operation == "describe-snapshots":
        command += ["--owner-ids", "self"]
    if operation == "describe-instance-status":
        command += ["--include-all-instances"]
    result = subprocess.run(
        command,
        capture_output=True, text=True, encoding="utf-8", timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env={**os.environ, "AWS_MAX_ATTEMPTS": "1", "AWS_PAGER": "",
             "AWS_EC2_METADATA_DISABLED": "true"})
    if result.returncode:
        # Never expose provider stderr: it may contain a URL or account details.
        raise ValueError("AWS read unavailable")
    if len(result.stdout) > 2_000_000:
        raise ValueError("Inventory too large")
    return json.loads(result.stdout)


def aws_inventory():
    scopes = []
    expected_account = os.environ.get("VEGA_AWS_ACCOUNT_ID", "")
    if not re.fullmatch(r"\d{12}", expected_account):
        raise ValueError("Configure an AWS account ID before requesting its inventory.")
    if aws("sts", "get-caller-identity", "us-east-1", "Account") != expected_account:
        raise ValueError("AWS account differs")
    regions = aws("ec2", "describe-regions", "us-east-1", "Regions[].RegionName")
    lightsail = aws("lightsail", "get-regions", "us-east-1", "regions[].name")
    if (not isinstance(regions, list) or not 1 <= len(regions) <= 40
            or not isinstance(lightsail, list) or len(lightsail) > 40):
        raise ValueError("Region inventory unavailable")
    ec2_query = ("Reservations[].Instances[].{id:InstanceId,type:InstanceType,state:State.Name,"
                 "name:Tags[?Key=='Name']|[0].Value,zone:Placement.AvailabilityZone,"
                 "platform:PlatformDetails,launched:LaunchTime,"
                 "disks:BlockDeviceMappings[].{device:DeviceName,id:Ebs.VolumeId,"
                 "delete_on_termination:Ebs.DeleteOnTermination}}")
    ls_query = ("instances[].{id:name,name:name,type:bundleId,state:state.name,"
                "zone:location.availabilityZone,platform:blueprintName,"
                "ram_gb:hardware.ramSizeInGb,cpus:hardware.cpuCount,"
                "disks:hardware.disks[].{size_gb:sizeInGb,system:isSystemDisk}}")
    requests = [("ec2", "describe-instances", region, ec2_query) for region in regions]
    requests += [("lightsail", "get-instances", region, ls_query) for region in lightsail]
    requests += [("s3api", "list-buckets", "us-east-1", "Buckets[].{name:Name,created:CreationDate}")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(aws, *request): request for request in requests}
        for job in as_completed(jobs):
            service, _, region, _ = jobs[job]
            service = "s3" if service == "s3api" else service
            scope = {"id": f"aws:{service}:{region}", "provider": "aws",
                     "service": service, "region": region, "checked_at": now()}
            try:
                rows = job.result()
                if not isinstance(rows, list) or len(rows) > 1000:
                    raise ValueError("Invalid metadata")
                # Output fields are restricted by the fixed JMESPath queries.
                scope.update(ok=True, rows=rows)
            except Exception:
                scope.update(ok=False, error="Read unavailable", rows=[])
            scopes.append(scope)
    occupied = {scope["region"] for scope in scopes
                if scope["service"] == "ec2" and scope["ok"] and scope["rows"]}
    scopes.extend(disk_inventory(occupied))
    scopes.extend(status_inventory(occupied))
    return scopes


def status_inventory(regions):
    """Only occupied, already discovered regions; never start or recover hosts."""
    results = []
    query = ("InstanceStatuses[].{id:InstanceId,state:InstanceState.Name,"
             "system:SystemStatus.Status,instance:InstanceStatus.Status,ebs:AttachedEbsStatus.Status}")
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {pool.submit(aws, "ec2", "describe-instance-status", region, query): region for region in regions}
        for job in as_completed(jobs):
            region = jobs[job]
            scope = {"id": f"aws:instance-status:{region}", "provider": "aws", "service": "instance-status",
                     "region": region, "checked_at": now()}
            try:
                rows = job.result()
                if not isinstance(rows, list) or len(rows) > 1000:
                    raise ValueError("Invalid status metadata")
                scope.update(ok=True, rows=rows)
            except Exception:
                scope.update(ok=False, rows=[], error="Server checks unavailable")
            results.append(scope)
    return results


def disk_inventory(regions):
    results = []
    queries = {
        "volumes": "Volumes[].{id:VolumeId,size_gb:Size,type:VolumeType,encrypted:Encrypted,state:State,zone:AvailabilityZone}",
        "snapshots": "Snapshots[].{id:SnapshotId,volume:VolumeId,size_gb:VolumeSize,state:State,started:StartTime,encrypted:Encrypted}",
    }
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {pool.submit(aws, "ec2", "describe-" + kind, region, query): (region, kind)
                for region in regions for kind, query in queries.items()}
        for job in as_completed(jobs):
            region, kind = jobs[job]
            result = {"id": f"aws:{kind}:{region}", "provider": "aws", "service": kind,
                      "region": region, "checked_at": now()}
            try:
                rows = job.result()
                if not isinstance(rows, list) or len(rows) > 1000:
                    raise ValueError("Invalid metadata")
                result.update(ok=True, rows=rows)
            except Exception:
                result.update(ok=False, rows=[], error="Read unavailable")
            results.append(result)
    return results


def cloudflare_inventory():
    account = _cloudflare_account()
    token = tomllib.loads(_cloudflare_auth_path().read_text(encoding="utf-8"))["oauth_token"]
    opener = build_opener(NoRedirect())

    def get(path):
        request = Request("https://api.cloudflare.com/client/v4/" + path,
                          headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                          method="GET")
        with opener.open(request, timeout=12) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Inventory too large")
        value = json.loads(raw)
        if value.get("success") is not True:
            raise ValueError("Read unavailable")
        return value

    requests = {
        "r2": f"accounts/{account}/r2/buckets",
        "pages": f"accounts/{account}/pages/projects",
        "tunnels": f"accounts/{account}/cfd_tunnel?is_deleted=false&per_page=100",
        "workers": f"accounts/{account}/workers/scripts",
        "zones": f"zones?account.id={account}&per_page=100",
    }
    scopes = []
    rate_limited = False
    for service, path in requests.items():
        scope = {"id": "cloudflare:" + service, "provider": "cloudflare", "service": service,
                 "checked_at": now()}
        if rate_limited:
            scope.update(ok=False, rows=[], error="Rate limited; check again later")
            scopes.append(scope)
            continue
        try:
            value = get(path)
            result = value["result"]
            rows = result.get("buckets", []) if service == "r2" else result
            if not isinstance(rows, list):
                raise ValueError("Invalid inventory")
            info = value.get("result_info") or {}
            complete = not info.get("is_truncated") and info.get("total_pages", 1) <= 1
            if service == "pages":
                # Pages defaults to ten projects. Follow only this fixed
                # metadata endpoint, bounded to ten pages.
                total = int(info.get("total_pages", 1))
                for page in range(2, min(total, 10) + 1):
                    next_page = get(path + f"?page={page}")
                    if not isinstance(next_page.get("result"), list):
                        raise ValueError("Invalid page")
                    rows.extend(next_page["result"])
                complete = total <= 10
            cleaned = []
            for row in rows:
                item = {"name": text(row.get("name") or row.get("id"))}
                if service == "tunnels":
                    item["state"] = text(row.get("status") or "unknown")
                if service == "pages":
                    latest = row.get("canonical_deployment") or row.get("latest_deployment") or {}
                    item["deployment_state"] = text((latest.get("latest_stage") or {}).get("status"))
                    item["domains"] = [text(x, 180) for x in row.get("domains", [])[:12]]
                if service == "zones":
                    item["state"] = text(row.get("status") or "unknown")
                if service == "r2":
                    item["created"] = text(row.get("creation_date"))
                cleaned.append(item)
            # Do not call a possibly truncated list a complete inventory.
            if len(rows) >= 100 and service in ("tunnels", "zones"):
                complete = False
            scope.update(ok=True, rows=cleaned, complete=complete)
        except Exception as error:
            rate_limited = isinstance(error, HTTPError) and error.code == 429
            scope.update(ok=False, rows=[], error="Rate limited; check again later" if rate_limited
                         else "Access not available" if isinstance(error, HTTPError) else "Read unavailable")
        scopes.append(scope)
    return scopes


def collect():
    result = {"version": 1, "started_at": now(), "scopes": [], "errors": []}
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {pool.submit(function): label
                for label, function in (("AWS", aws_inventory), ("Cloudflare", cloudflare_inventory))}
        for job in as_completed(jobs):
            try:
                result["scopes"].extend(job.result())
            except Exception:
                result["errors"].append({"provider": jobs[job], "error": "Credentials or inventory unavailable"})
    result["finished_at"] = now()
    return result


def save(result, path=ROOT / ".state" / "infrastructure-current.json"):
    path = Path(path)
    # A failed read is not evidence of deletion. Keep one last successful
    # observation, explicitly separate from this attempt. No backup copies.
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            raise ValueError("Invalid prior observation")
        previous_scopes = {s["id"]: s for s in previous.get("scopes", [])}
    except (OSError, ValueError, TypeError, KeyError):
        previous_scopes = {}
    for scope in result["scopes"]:
        if not scope.get("ok"):
            old = previous_scopes.get(scope["id"], {})
            successful = old if old.get("ok") else old.get("last_success")
            if successful:
                scope["last_success"] = {k: v for k, v in successful.items() if k != "last_success"}
    for old in previous_scopes.values():
        if old["id"] not in {s["id"] for s in result["scopes"]}:
            successful = old if old.get("ok") else old.get("last_success")
            if successful:
                result["scopes"].append({"id": old["id"], "service": old["service"], "provider": old.get("provider"),
                    "region": old.get("region"), "ok": False, "rows": [], "checked_at": result.get("finished_at", now()),
                    "error": "Provider read unavailable", "last_success": successful})
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    result = collect()
    save(result)
    print(json.dumps({"finished_at": result["finished_at"], "errors": result["errors"],
                      "scopes": [{"id": s["id"], "ok": s["ok"], "rows": len(s["rows"])}
                                 for s in result["scopes"]]}, indent=2))

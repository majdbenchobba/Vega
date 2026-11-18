"""Read-only previews of references actually present in an owned receipt."""
import re

REFERENCE = re.compile(
    r"\b(?:prior[a-f0-9-]{8,36}|turn[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
    r"|(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.(?:md|txt))\b"
)


def preview_reference(owner, request_id, reference, *, requests, reports, tasks, activity):
    source = requests.get(owner, request_id)
    snapshot = reports._snapshot(owner)
    if not snapshot["principal"]["enabled"]:
        raise PermissionError("Project context is disabled.")
    text = "\n".join([source.get("text", "")] + [r.get("receipt") or "" for r in source["routes"]])
    if not isinstance(reference, str) or reference not in REFERENCE.findall(text):
        raise ValueError("Choose a reference from this saved receipt.")
    result = {"label": reference, "available": False,
              "text": "This historical reference is not available in the current project connections."}
    if reference.startswith("prior"):
        prefix = reference[5:]
        with requests.store.db() as db:
            requests._principal(db, owner)
            rows = db.execute("SELECT id FROM project_requests WHERE owner=? AND id LIKE ? LIMIT 2",
                              (owner, prefix + "%")).fetchall()
        if len(rows) == 1:
            previous = requests.get(owner, rows[0]["id"])
            result.update(available=True, label="Earlier request", text=previous["text"],
                          state=previous["state"], recorded_at=previous["created_at"])
    elif reference.startswith("turn"):
        identity = reference[4:]
        aliases = [alias for alias, value in tasks.items() if value.get("thread_id") == identity]
        if len(aliases) == 1:
            alias = aliases[0]
            current = activity.views(owner).get(alias, {})
            result.update(label=tasks[alias]["label"], available=bool(current.get("available")),
                          text=current.get("latest_update", {}).get("text") if current.get("latest_update") else
                          "No current public update is available for this chat.",
                          recorded_at=current.get("observed_at"), state=current.get("label"))
        else:
            result["text"] = "This refers to an older chat. It is no longer connected. See Current project activity for the replacement chat; the original receipt is kept below."
    else:
        entries = [entry for entry in snapshot["catalog"]["reports"]
                   if reference in [entry["path"], *entry.get("known_paths", [])]]
        if len(entries) == 1:
            report = reports.read(owner, {"kind": "report", "report_id": entries[0]["id"]})
            result.update(label=entries[0]["label"], available=report.get("available", False),
                          text=report.get("text") or report.get("source_error") or "Document unavailable.",
                          recorded_at=report.get("observed_at") or report.get("retrieved_at"))
    reports._recheck(snapshot)
    return result

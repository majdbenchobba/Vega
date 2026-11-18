"""A small read-only projection for Vega's project map and status board."""
from __future__ import annotations

from work_activity import agent_activity


def overview(vega, owner, project=None):
    catalog = vega.team.catalog(owner, project=project, include_file_access=False)
    observed = vega.project_views(owner)
    projects = [{key: row.get(key) for key in
                 ("alias", "label", "status", "observed_at", "freshness", "checklist", "progress_assessment",
                  "connection", "routing_owner", "codex_activity", "chat_activity")}
                for row in observed["projects"]]
    runs = []
    for run in catalog["runs"]:
        agents = [{key: agent.get(key) for key in ("id", "role", "label", "state", "model_requests", "error")}
                  for agent in run.get("agents", [])]
        review = run.get("vega_review") or {}
        planner = next((node for node in run.get("nodes", []) if node["id"] == "planner"), {})
        checkpoints = (int(planner.get("state") == "completed")
                       + sum(agent["state"] == "completed" for agent in agents)
                       + int(review.get("model_reviewed") is True))
        item = {key: run.get(key) for key in
                ("id", "project", "label", "mode", "goal", "state", "created_at", "updated_at", "error", "usage")}
        item.update(agents=agents, review={key: review.get(key) for key in ("status", "model_reviewed", "summary")},
                    activity=run.get("activity") or agent_activity(run),
                    progress={"completed": checkpoints, "total": 4, "label": "Review steps"}
                    if run.get("delegation_version") == 1 else None)
        runs.append(item)
    requests = []
    for request in catalog["requests"]:
        # Use a separate card per target. One handled target never paints its
        # siblings as routed or implemented.
        for target in request["targets"]:
            if project and target["alias"] != project:
                continue
            route = next((item for item in request.get("routes", [])
                          if item["task"] == target["alias"] and item["thread_id"] == target["thread_id"]), None)
            state = route["state"] if route else "awaiting_input" if request["state"] == "awaiting_input" else "stale_target"
            requests.append({"id": request["id"], "project": target["alias"], "label": target["label"],
                             "instruction": (request.get("instruction") or request.get("text") or "")[:160],
                             "state": state, "created_at": request.get("created_at"),
                             "route_receipt": {key: route.get(key) for key in
                                               ("state", "receipt", "handled_at", "update_id")} if route else None})
    return {"version": 1, "snapshot_at": catalog["snapshot_at"], "work_paused": catalog["work_paused"],
            "projects": projects, "selected_project": project, "runs": runs, "requests": requests,
            "agents": catalog["agents"], "skills": catalog["skills"],
            "automation": vega.n8n.local_overview(owner, project=project),
            "limits": {"recent_runs": 30, "recent_requests": 30}}

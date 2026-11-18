"""Read-only work labels derived from saved state and structured receipts."""
import re


def agent_activity(run):
    state=run.get("state")
    proposal=run.get("proposal") or {}
    result=run.get("result") or {}
    digest=result.get("applied_sha256")
    applied=proposal.get("state")=="applied"
    verified=bool(
        applied and state=="completed" and result.get("implementation")=="applied_verified"
        and isinstance(digest,str) and re.fullmatch(r"[a-f0-9]{64}",digest)
        and digest==proposal.get("after_sha256")
    )
    if verified:
        return {"stage":"applied_verified","label":"Change applied · file verified",
                "implementation_verified":True,"needs_attention":False}
    if applied:
        return {"stage":"applied_unverified","label":"Applied · verification needs attention",
                "implementation_verified":False,"needs_attention":True}
    if state=="completed":
        label={"report":"Report ready","inspect":"Inspection complete",
               "review":"Review complete","automation":"Workflow draft ready"}.get(run.get("mode"),"Result ready")
    else:
        label={
            "queued":"Queued","planning":"Planning","reading":"Reading sources",
            "delegating":"Specialists working",
            "preparing":"Preparing a change","reviewing":"Reviewing","verifying":"Checking",
            "awaiting_approval":"Needs your review","applying":"Applying the reviewed change",
            "failed":"Needs attention","interrupted":"Interrupted","expired":"Expired",
            "cancelled":"Cancelled",
            "needs_revision":"Vega requested revisions","review_blocked":"Vega review blocked",
        }.get(state,"Status unavailable")
    return {"stage":state or "unknown","label":label,"implementation_verified":False,
            "needs_attention":state in ("awaiting_approval","failed","interrupted","expired","needs_revision","review_blocked")}


def request_activity(request):
    state=request.get("state")
    label={
        "awaiting_input":"Needs details","queued":"Queued for routing","routing":"Being routed",
        "handled":"Routed","stale_target":"Destination needs review",
        "cancelled":"Cancelled","partially_cancelled":"Partly cancelled",
    }.get(state,"Routing status unavailable")
    return {"stage":"routed" if state=="handled" else state or "unknown","label":label,
            "implementation_verified":False,
            "needs_attention":state in ("awaiting_input","stale_target","partially_cancelled")}


def conversation_activity(catalog):
    """Reuse the same evidence labels in Vega's conversational work updates."""
    lines=[]
    for run in catalog.get("runs",[])[:5]:
        activity=run.get("activity") or agent_activity(run)
        line=f"• {run.get('label') or run.get('project')}: {activity['label']}"
        if run.get("path"):
            line+=" — "+str(run["path"])[:160]
        lines.append(line)
    for request in catalog.get("requests",[])[:5]:
        labels=", ".join(target.get("label") or target["alias"] for target in request.get("targets",[]))
        activity=request.get("activity") or request_activity(request)
        lines.append(f"• {labels or 'Project request'}: {activity['label']}")
    if catalog.get("work_paused"):
        lines.append("Automatic project continuation is paused.")
    return "\n".join(lines) if lines else "No project work is recorded yet."

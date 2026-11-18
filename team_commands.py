"""Explicit current-owner team commands; source/history never creates a run."""
import re
from memory_policy import unquoted_text


def team_command(text, projects):
    if not isinstance(text,str):return None
    current=unquoted_text(text).strip()
    if current!=text.strip():return None
    if re.fullmatch(r"/team(?:\s+help)?",current,re.I):return {"operation":"help"}
    status=re.fullmatch(r"/team\s+status\s+([a-f0-9-]{36})",current,re.I)
    if status:return {"operation":"status","id":status[1]}
    match=re.fullmatch(r"/(?:team|agents)\s+(report|review|change|automation)\s+([a-z0-9-]+)\s+(.+)",current,re.I|re.S)
    if not match:return None
    mode,project,content=match[1].lower(),match[2].lower(),match[3].strip()
    if project not in projects:raise ValueError("Choose a currently registered project.")
    if mode in {"report","automation"}:
        return {"operation":"create","mode":mode,"project":project,"goal":content}
    selected=re.fullmatch(r"(\S+)\s+(.+)",content,re.S)
    if not selected:raise ValueError("Include a relative file path and the task.")
    return {"operation":"create","mode":mode,"project":project,"path":selected[1],"goal":selected[2]}

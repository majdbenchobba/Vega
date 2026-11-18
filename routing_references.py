"""Adopt referenced text only after a current, explicit owner routing directive."""
import re
from memory_policy import contains_secret, unquoted_text

ORIGINS={"typed","telegram_text","manual_review","speaker_matched"}


def _same_owner(item,owner):
    return (isinstance(item,dict) and item.get("owner_id")==owner
            and item.get("source") in {"local","telegram","telegram_voice"})


def _prompt(text):
    if not isinstance(text,str) or not 20<=len(text)<=12000 or contains_secret(text):return None
    fenced=re.fullmatch(r"[\s\S]*?```(?:text|markdown|md)?\s*\n([\s\S]+?)\n```[ \t\n]*",text)
    if fenced and len(re.findall(r"(?m)^```",text))==2:return fenced[1]
    heading=re.search(r"(?m)^(?:#{1,3}\s+|\*\*)(?:Coordinator|Vega Main|Instruction|Prompt|Fix|Implement)\b",text)
    if heading and re.search(r"(?m)^(?:Scope:|\d+\.\s|[-*]\s)",text[heading.start():]):
        return text[heading.start():].strip()
    return None


def resolve_reference(current,history,permissions):
    if (not isinstance(current,dict) or current.get("origin") not in ORIGINS
            or not permissions.get("routing_requested")
            or not permissions.get("routing_reference_requested")
            or permissions.get("routing_target_unresolved")):
        return None
    owner=current.get("owner_id")
    cutoff=current.get("received_at",current.get("created_at"))
    if type(owner) is not int or type(cutoff) not in (int,float):return None
    prior=[item for item in history[-20:] if _same_owner(item,owner)
           and type(item.get("created_at")) in (int,float)
           and 0<=cutoff-item["created_at"]<=1800
           and item.get("update_id")!=current.get("update_id")]
    owner_rows={str(item["update_id"]):item for item in prior
                if item.get("role")=="user" and item.get("origin") in ORIGINS}
    # An explicit send can adopt an immediately preceding copyable assistant
    # prompt as data. That prompt's authoring owner turn must also be present.
    for item in reversed(prior):
        if item.get("role")=="assistant":
            parent=owner_rows.get(str(item.get("update_id")))
            body=_prompt(item.get("text")) if parent else None
            if body:
                source_ids=[parent["update_id"]]
                if re.match(r"(?i)^(?:please\s+)?(?:send|submit|put|forward|route)\b",parent.get("text","")):
                    for candidate in reversed(prior):
                        if (candidate.get("role")=="user" and candidate.get("origin") in ORIGINS
                                and candidate["created_at"]<parent["created_at"]
                                and len(unquoted_text(candidate.get("text","")).strip())>=20):
                            source_ids.append(candidate["update_id"])
                            break
                return {"instruction":body,"source_update_ids":source_ids,
                        "referenced_message_update_id":item["update_id"],"kind":"adopted_prompt"}
        if item.get("role")=="user" and item.get("origin") in ORIGINS:
            body=item.get("text","")
            clean=unquoted_text(body).strip()
            if not clean or contains_secret(body):return None
            if re.match(r"(?i)^(?:please\s+)?(?:send|submit|put|forward|route)\b",clean):
                continue
            if re.match(r"(?i)^(?:thanks|thank you|hi|hello|good morning|okay|ok)[.! ]*$",clean):
                return None
            # Only the most recent substantive owner message may be adopted.
            if len(clean)<20 or len(body)>12000:return None
            return {"instruction":body,"source_update_ids":[item["update_id"]],
                    "referenced_message_update_id":item["update_id"],"kind":"owner_request"}
    return None

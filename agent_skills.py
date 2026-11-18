"""Bounded source-analysis skills; no file access, imports from projects or execution."""
from __future__ import annotations

import ast
from html.parser import HTMLParser
import json
from pathlib import PureWindowsPath
import re
import tomllib


AGENTS = [
    {"id":"project_researcher","label":"Project researcher",
     "description":"Investigates project status and delivery evidence.",
     "skills":["project.snapshot","project.receipts","files.read"],"artifact_kind":"analysis"},
    {"id":"code_analyst","label":"Code analyst",
     "description":"Inspects code structure, dependencies and interactions.",
     "skills":["files.read","workspace.read","code.outline","dependencies.inspect"],"artifact_kind":"analysis"},
    {"id":"quality_analyst","label":"Quality analyst",
     "description":"Finds existing tests and drafts concrete checks for the requested behavior.",
     "skills":["files.read","code.outline","tests.inventory","tests.design"],"artifact_kind":"test_plan"},
    {"id":"interface_analyst","label":"Interface analyst",
     "description":"Reviews interface source and accessibility structure; does not claim a browser test.",
     "skills":["files.read","code.outline","ui.structure","tests.design"],"artifact_kind":"analysis"},
    {"id":"documentation_writer","label":"Documentation writer",
     "description":"Produces a documentation draft grounded in the supplied sources.",
     "skills":["files.read","project.snapshot","docs.draft"],"artifact_kind":"documentation"},
    {"id":"patch_author","label":"Patch author",
     "description":"Prepares a concrete single-file diff for Vega review and then owner approval.",
     "skills":["workspace.read","change.prepare","syntax.check"],"artifact_kind":"change_proposal"},
    {"id":"automation_designer","label":"Automation designer",
     "description":"Designs a local n8n project workflow for Vega review and inactive publication.",
     "skills":["project.snapshot","automation.catalog","automation.draft","automation.validate"],
     "artifact_kind":"automation_workflow"},
]
AGENT_BY_ID = {agent["id"]:agent for agent in AGENTS}
MODE_AGENTS = {
    "report":["project_researcher","documentation_writer","quality_analyst"],
    "inspect":["code_analyst","quality_analyst","interface_analyst","documentation_writer","project_researcher"],
    "review":["code_analyst","quality_analyst","interface_analyst","documentation_writer"],
    "change":["patch_author","quality_analyst","interface_analyst","code_analyst","documentation_writer"],
    "automation":["automation_designer","quality_analyst"],
}
SOURCE_SKILLS = {"code.outline","dependencies.inspect","tests.inventory","ui.structure"}
MAX_CITATION_LINES = 40


def _short(value, limit=160):
    return str(value)[:limit]


class InterfaceInventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items=[]

    def handle_starttag(self, tag, attrs):
        attributes=dict(attrs)
        if tag in {"input","button","select","textarea","img","label","h1","h2","h3","nav","dialog"}:
            self.items.append({"kind":tag,"line":self.getpos()[0],
                "id":_short(attributes.get("id") or "",80),
                "label":_short(attributes.get("aria-label") or attributes.get("aria-labelledby") or "",100),
                "type":_short(attributes.get("type") or "",40)})


def _outline(path, text):
    suffix=PureWindowsPath(path).suffix.lower()
    if suffix==".py":
        tree=ast.parse(text)
        return [{"kind":type(node).__name__,"name":node.name,"line":node.lineno}
                for node in ast.walk(tree) if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
    if suffix==".json":
        value=json.loads(text)
        return [{"kind":"field","name":_short(key)} for key in value] if isinstance(value,dict) else []
    if suffix in {".js",".mjs",".cjs",".jsx",".ts",".tsx"}:
        pattern=re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function|class|const|let)\s+([A-Za-z_$][\w$]*)",re.M)
        return [{"kind":"declaration","name":match[1],"line":text.count("\n",0,match.start())+1}
                for match in pattern.finditer(text)]
    return []


def _dependencies(path, text):
    name=PureWindowsPath(path).name.lower()
    if name=="package.json":
        value=json.loads(text)
        return [{"kind":section,"name":_short(key),"version":_short(version)}
                for section in ("dependencies","devDependencies","peerDependencies","optionalDependencies")
                for key,version in (value.get(section,{}) or {}).items()]
    if name=="pyproject.toml":
        value=tomllib.loads(text)
        items=[{"kind":"dependency","name":_short(item)} for item in value.get("project",{}).get("dependencies",[])]
        items.extend({"kind":"build_dependency","name":_short(item)}
                     for item in value.get("build-system",{}).get("requires",[]))
        return items
    if name.startswith("requirements") and name.endswith(".txt"):
        return [{"kind":"requirement","name":_short(line.strip()),"line":index}
                for index,line in enumerate(text.splitlines(),1) if line.strip() and not line.lstrip().startswith("#")]
    return []


def _tests(path, text):
    if PureWindowsPath(path).suffix.lower()==".py":
        return [{"kind":"test_definition","name":node.name,"line":node.lineno}
                for node in ast.walk(ast.parse(text))
                if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith("test_")]
    pattern=re.compile(r"\b(test|it|describe)(?:\.(?:only|skip|todo))?\s*\(\s*(['\"`])([^\r\n]{1,160}?)\2")
    return [{"kind":match[1],"name":match[3],"line":text.count("\n",0,match.start())+1}
            for match in pattern.finditer(text)]


def analyze_sources(skill, sources):
    if skill not in SOURCE_SKILLS:
        raise ValueError("This source-analysis skill is unavailable.")
    facts,limitations=[],[]
    for source in sources[:5]:
        if source.get("kind")!="file" or not isinstance(source.get("text"),str):
            continue
        path,text=source.get("path",""),source["text"]
        if len(text)>16000:
            raise ValueError("Source text exceeds the analysis limit.")
        if source.get("truncated"):
            limitations.append("Only the provided source excerpt was inspected.")
        try:
            if skill=="code.outline":
                items=_outline(path,text)
            elif skill=="dependencies.inspect":
                items=_dependencies(path,text)
                limitations.append("Dependency declarations were read; packages were not installed or checked online.")
            elif skill=="tests.inventory":
                items=_tests(path,text)
                limitations.append("Static test declarations are not executed test results.")
            else:
                if PureWindowsPath(path).suffix.lower() in {".html",".htm"}:
                    parser=InterfaceInventory();parser.feed(text);items=parser.items
                else:
                    items=[]
                limitations.append("Interface source was inspected; no rendered page or browser interaction was tested.")
            facts.extend({**item,"source_id":source["id"],"path":path} for item in items[:24])
        except (SyntaxError,ValueError,TypeError,AttributeError,RecursionError):
            limitations.append("One source could not be completely parsed; no complete inventory is claimed.")
    total=len(facts)
    facts=facts[:32]
    while len(json.dumps(facts,ensure_ascii=False))>2600 and facts:
        facts.pop()
    if not facts:
        limitations.append("No matching declarations were found in the supplied source excerpts.")
    return {"skill":skill,"summary":f"Recorded {len(facts)} bounded source findings.",
            "facts":facts,"truncated":len(facts)<total,"limitations":list(dict.fromkeys(limitations))}


def validate_citations(citations, sources, *, allowed_ids=None, max_excerpt_chars=4000):
    """Return exact quoted source ranges, never a model-supplied quotation."""
    if not isinstance(citations,list) or not 1<=len(citations)<=6:
        raise ValueError("An agent must cite its supplied evidence.")
    lookup={source["id"]:source for source in sources}
    allowed=set(lookup) if allowed_ids is None else set(allowed_ids)
    result=[]
    remaining=max_excerpt_chars
    for citation in citations:
        if not isinstance(citation,dict) or set(citation)!={"source_id","start_line","end_line"}:
            raise ValueError("Use bounded source citations.")
        identity,start,end=citation["source_id"],citation["start_line"],citation["end_line"]
        if identity not in allowed or identity not in lookup:
            raise ValueError("An agent cited a source outside its assignment.")
        source=lookup[identity]
        lines=source["text"].splitlines() or [""]
        if (type(start) is not int or type(end) is not int
                or not 1<=start<=end<=len(lines) or end-start>=MAX_CITATION_LINES):
            raise ValueError("An agent cited an unavailable source range.")
        full="\n".join(lines[start-1:end])
        excerpt=full[:min(1200,remaining)]
        remaining-=len(excerpt)
        result.append({"source_id":identity,"path":source.get("path"),"sha256":source["sha256"],
                       "start_line":start,"end_line":end,"excerpt":excerpt,
                       "excerpt_truncated":len(excerpt)<len(full)})
    return result

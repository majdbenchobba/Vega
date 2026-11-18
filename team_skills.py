"""Implemented Vega skills. A catalog entry never grants additional authority."""
import ast
import json
from pathlib import Path
import subprocess
import os

SKILLS = [
    {"id":"files.discover","label":"Find relevant files","description":"Find files from the owner request across authorized local folders.","writes":"none"},
    {"id":"files.read","label":"Read local sources","description":"Read relevant local source files and retain paths, hashes and search limits.","writes":"none"},
    {"id":"project.snapshot","label":"Project research","description":"Read the selected project's registered status, progress and replies.","writes":"none"},
    {"id":"project.receipts","label":"Receipt verification","description":"Check saved routing and delivery evidence for the selected project.","writes":"none"},
    {"id":"workspace.read","label":"Code inspection","description":"Read one selected file in an already registered local workspace.","writes":"none"},
    {"id":"change.prepare","label":"Prepare a change","description":"Prepare an exact single-file proposal using the configured model.","writes":"review_required"},
    {"id":"syntax.check","label":"Syntax verification","description":"Parse supported source text without executing the project.","writes":"none"},
    {"id":"change.verify","label":"Verify applied bytes","description":"Check an owner-applied change against its approved hash.","writes":"none"},
    {"id":"code.outline","label":"Map code structure","description":"Inventory declarations in the supplied code without executing it.","writes":"none"},
    {"id":"dependencies.inspect","label":"Inspect dependencies","description":"Read dependency declarations from supplied manifests; no installation or online lookup.","writes":"none"},
    {"id":"tests.inventory","label":"Find test declarations","description":"Locate existing test definitions in supplied source; does not run tests.","writes":"none"},
    {"id":"ui.structure","label":"Inspect interface structure","description":"Inspect supplied markup structure; does not claim rendered browser validation.","writes":"none"},
    {"id":"tests.design","label":"Draft test cases","description":"A quality specialist produces concrete cases and expected outcomes for Vega review.","writes":"artifact_only"},
    {"id":"docs.draft","label":"Draft documentation","description":"A documentation specialist creates a source-grounded draft without changing project files.","writes":"artifact_only"},
    {"id":"evidence.cite","label":"Verify source citations","description":"Validate each agent's references against its assigned source snapshot.","writes":"none"},
    {"id":"automation.catalog","label":"Explore automation templates","description":"Read the supported local n8n workflows and their project scope.","writes":"none"},
    {"id":"automation.draft","label":"Draft an n8n workflow","description":"Build a complete local project workflow for Vega review, then owner approval.","writes":"artifact_only"},
    {"id":"automation.validate","label":"Validate workflow scope","description":"Check the complete graph, project endpoint, trigger and absence of external actions.","writes":"none"},
]

MODES = {
    "report": {"specialist":"Research specialist","skills":["project.snapshot","project.receipts"]},
    "inspect": {"specialist":"Research specialist","skills":["files.discover","files.read","project.receipts"]},
    "review": {"specialist":"Engineering specialist","skills":["workspace.read","project.receipts","syntax.check"]},
    "change": {"specialist":"Engineering specialist","skills":["files.discover","workspace.read","project.receipts","change.prepare","syntax.check","change.verify"]},
    "automation": {"specialist":"Automation designer","skills":["project.snapshot","automation.catalog","automation.draft","automation.validate"]},
}


def verify_syntax(path, text, *, node=None):
    """No imports, test execution, shell interpolation or model-written commands."""
    if not isinstance(text,str) or len(text)>32_000:
        return {"state":"not_checked","detail":"Source exceeds the bounded syntax-check size."}
    suffix=Path(path).suffix.lower()
    try:
        if suffix==".py":
            ast.parse(text,filename=path)
        elif suffix==".json":
            json.loads(text)
        elif suffix in {".js",".mjs",".cjs"} and node:
            args=[str(node),"--check"]
            if suffix!=".cjs":args+=["--input-type=module"]
            env={**os.environ,"NODE_OPTIONS":""}
            result=subprocess.run(args,input=text,capture_output=True,text=True,encoding="utf-8",
                                  timeout=5,env=env,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
            if result.returncode:
                return {"state":"failed","detail":"JavaScript syntax check failed. Review the prepared source."}
        else:
            return {"state":"not_checked","detail":"No executable syntax check is registered for this file type."}
    except (SyntaxError,ValueError,RecursionError):
        return {"state":"failed","detail":"Source syntax is invalid."}
    except (OSError,subprocess.SubprocessError):
        return {"state":"unavailable","detail":"The local syntax checker was unavailable."}
    return {"state":"passed","detail":"Syntax parsed successfully; project behavior was not executed."}

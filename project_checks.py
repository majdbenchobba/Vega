"""Fixed local verification recipes; no model-supplied commands or test paths."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
RECIPE = "vega-speech-routes-v1"
INPUTS = ("test_speech_routes.py", "speech_service.py", "speech-worker.mjs",
          "port/src/main/vega/awsSpeech.ts", "aws_profile_credentials.mjs")
NODE = ROOT / ".n8n-runtime/node-v24.21.0-win-x64/node.exe"
ACTIVITY_INPUTS = (
    "codex_activity.py", "companion.py", "document_delivery.py", "media_store.py",
    "memory_policy.py", "memory_store.py", "notification_privacy.py", "photo_batches.py",
    "project_activity.py", "project_checklists.py", "project_files.py", "project_freshness.py",
    "project_milestones.py", "project_progress.py", "project_reports.py", "project_requests.py",
    "project_responses.py", "relay.py", "routing_targets.py", "telegram_formatting.py",
    "text_delivery.py", "transport_diagnostics.py", "work_activity.py", "work_overview.py",
    "test_project_activity.py", "test_project_freshness.py", "test_work_activity.py", "test_work_overview.py",
)
RECIPES = {
    RECIPE: {
        "label": "Speech checks", "runner": "python",
        "paths": ("standalone-host/speech-worker.mjs",),
        "tests": ("test_speech_routes",), "inputs": INPUTS,
    },
    "vega-project-activity-v1": {
        "label": "Project activity checks", "runner": "python",
        "paths": tuple("standalone-host/" + name for name in
                       ("project_activity.py", "project_freshness.py", "work_activity.py", "work_overview.py")),
        "tests": ("test_project_activity", "test_project_freshness", "test_work_activity", "test_work_overview"),
        "inputs": ACTIVITY_INPUTS,
    },
    "vega-project-map-v1": {
        "label": "Project map checks", "runner": "node",
        "paths": ("standalone-host/public/work-overview.js",),
        "tests": ("work_overview.test.mjs",),
        "inputs": ("work_overview.test.mjs", "public/work-overview.js", "public/reply-renderer.js",
                   "public/vendor/marked.js", "public/vendor/entities/decode.js",
                   "public/vendor/entities/decode-codepoint.js",
                   "public/vendor/entities/generated/decode-data-html.js",
                   "public/vendor/entities/generated/decode-data-xml.js",
                   "public/vendor/entities/internal/bin-trie-flags.js", "package.json", "package-lock.json"),
    },
    "vega-work-panel-v1": {
        "label": "Work panel checks", "runner": "node",
        "paths": ("standalone-host/public/team-panel.js",),
        "tests": ("team_activity.test.mjs",),
        "inputs": ("team_activity.test.mjs", "public/team-panel.js", "public/miniapp/app.js",
                   "public/miniapp/workspace.js", "public/miniapp/visuals.js",
                   "public/request-receipts.js",
                   "public/index.html", "public/dashboard.js", "public/voice-session.js",
                   "package.json", "package-lock.json"),
    },
}


def _recipe(identity):
    if not isinstance(identity, str) or identity not in RECIPES:
        raise ValueError("No registered verification recipe.")
    return RECIPES[identity]


def _target(identity, path):
    recipe = _recipe(identity)
    if path not in recipe["paths"]:
        raise ValueError("This file is outside the registered verification recipe.")
    return path.removeprefix("standalone-host/")


def recipe_for(project, path, workspace):
    if project == "vega" and Path(workspace).resolve() == ROOT.parent:
        for identity, recipe in RECIPES.items():
            if path in recipe["paths"]:
                return {"id": identity, "label": recipe["label"], "timeout_seconds": 90}
    return None


def input_hashes(recipe_id=RECIPE):
    names = ("project_checks.py", *_recipe(recipe_id)["inputs"])
    result = {}
    for name in names:
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError("A verification input moved outside Vega.")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def is_current(result, expected_sha256, *, recipe_id=RECIPE, path="standalone-host/speech-worker.mjs"):
    """A dated pass is reusable only for this recipe and these unchanged inputs."""
    try:
        target = _target(recipe_id, path)
        if (not isinstance(result, dict) or result.get("state") != "passed"
                or result.get("recipe") != recipe_id or result.get("source_sha256") != expected_sha256
                or type(result.get("tests")) is not int or result["tests"] < 1):
            return False
        current = input_hashes(recipe_id)
        return current[target] == expected_sha256 and result.get("input_sha256") == current
    except (OSError, ValueError, KeyError):
        return False


def _counts(output, runner):
    if runner == "python":
        match = re.search(rb"Ran (\d+) tests? in ", output)
        skipped = re.search(rb"skipped=(\d+)", output)
        return (int(match[1]) if match else 0) - (int(skipped[1]) if skipped else 0), True
    counts = {key.decode(): int(value) for key, value in
              re.findall(rb"^# (pass|fail|cancelled) (\d+)\s*$", output, re.M)}
    return counts.get("pass", 0), counts.get("fail") == 0 and counts.get("cancelled") == 0


def execute(expected_sha256, *, recipe_id=RECIPE, path="standalone-host/speech-worker.mjs", run=subprocess.run):
    recipe = _recipe(recipe_id)
    target = _target(recipe_id, path)
    before = input_hashes(recipe_id)
    if before[target] != expected_sha256:
        raise ValueError("The applied file changed before verification.")
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("PYTHON") or name in {"NODE_OPTIONS", "NODE_PATH"}:
            env.pop(name)
    started = time.time()
    command = ([sys.executable, "-B", "-m", "unittest", *recipe["tests"], "-q"]
               if recipe["runner"] == "python" else
               [str(NODE), "--test", "--test-reporter=tap", *recipe["tests"]])
    base = {"recipe": recipe_id, "started_at": started, "source_sha256": expected_sha256,
            "input_sha256": before}
    try:
        result = run(command, cwd=ROOT, env=env, capture_output=True, timeout=90,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output = result.stdout + result.stderr
        count, successful = _counts(output, recipe["runner"])
        try:
            unchanged = before == input_hashes(recipe_id)
        except (OSError, ValueError):
            unchanged = False
        passed = result.returncode == 0 and count > 0 and successful and unchanged
        return {**base, "state": "passed" if passed else "failed",
                "tests": max(0, count), "exit_code": result.returncode, "finished_at": time.time(),
                "inputs_unchanged": unchanged,
                "output_sha256": hashlib.sha256(output).hexdigest(),
                "detail": (f"{count} {recipe['label'].lower()} passed." if passed else
                           "Checks failed or the source changed. Review is needed.")}
    except subprocess.TimeoutExpired:
        return {**base, "state": "failed", "tests": None, "exit_code": None, "finished_at": time.time(),
                "detail": "Checks exceeded 90 seconds."}

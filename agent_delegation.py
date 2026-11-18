"""Independent specialist work followed by a separately admitted Vega review."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import difflib
import hashlib
import json

from agent_skills import AGENT_BY_ID, SOURCE_SKILLS, MAX_CITATION_LINES, analyze_sources, validate_citations
from companion import ConflictError
from conversation import ConversationError
from project_files import text_from_bytes
from team_skills import verify_syntax
from n8n_workflows import skill_catalog, workflow_spec, build_workflow, validate_workflow


ACTIVE={"planning","reading","delegating","preparing","reviewing","verifying"}
SETTLED={"completed","failed","cancelled","interrupted","expired","needs_revision","review_blocked"}


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(",",":"))


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()

def change_citations(source, proposed):
    """Cite the original lines changed, or insertion context, from the snapshot."""
    before = source["text"].splitlines() or [""]
    after = proposed.splitlines()
    ranges = []
    for kind, start, end, _, _ in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if kind == "equal":
            continue
        first = min(start + 1, len(before))
        last = max(first, end)
        for line in range(first, last + 1, MAX_CITATION_LINES):
            ranges.append({"source_id": source["id"], "start_line": line,
                           "end_line": min(last, line + MAX_CITATION_LINES - 1)})
    # The existing citation limit stops oversized changes instead of hiding hunks.
    return validate_citations(ranges, [source], allowed_ids=[source["id"]])


class DelegationRunner:
    def __init__(self,service,*,node=None):
        self.service,self.node=service,node

    def _source(self,identity,path,kind,text,*,sha=None,truncated=False,limit=12000):
        if not isinstance(text,str):
            text=encoded(text)
        original=text
        text=text[:limit]
        return {"id":identity,"path":path,"kind":kind,"text":text,
                "sha256":sha or hashlib.sha256(original.encode()).hexdigest(),
                "truncated":bool(truncated or len(text)<len(original))}

    def _sources(self,run,evidence,receipts):
        sources=[self._source("owner","Current owner request","owner",run["goal"],limit=2000),
                 self._source("receipts",run["project"]+" delivery receipts","receipts",receipts,limit=3500)]
        if run["mode"] in {"report","automation"}:
            sources.append(self._source("project",run["project"]+" project snapshot","project",
                                        evidence.get("project",evidence),limit=12000))
            if run["mode"]=="automation":
                sources.append(self._source("automation_catalog","Local n8n workflow catalog","automation",
                                            skill_catalog(run["project"]),limit=5000))
        else:
            files=[evidence["file"]] if "file" in evidence else evidence.get("files",[])
            remaining=12000
            for index,file in enumerate(files[:3]):
                text=file.get("text")
                kind="file" if isinstance(text,str) else "file_metadata"
                if not isinstance(text,str):
                    text=encoded({key:file.get(key) for key in ("path","status","reason","bytes")})
                identity="selected_file" if run["mode"] in {"change","review"} else f"file-{index+1}"
                sources.append(self._source(identity,file.get("path"),kind,text,
                    sha=file.get("sha256"),truncated=file.get("truncated",False),limit=max(0,remaining)))
                remaining-=len(sources[-1]["text"])
        return sources

    def _guard(self,db,run):
        return self.service._guard(db,run["owner"],run["id"],ACTIVE)

    def _save_plan(self,run,plan,sources):
        service=self.service
        manifest=[{key:source.get(key) for key in ("id","path","kind","sha256","truncated")} |
                  {"lines":len(source["text"].splitlines()) or 1} for source in sources]
        with service.store.db() as db:
            db.execute("BEGIN IMMEDIATE");self._guard(db,run)
            now=service.clock()
            db.execute("INSERT INTO team_delegations VALUES(?,?,?,?,?,?,?)",
                       (run["id"],encoded(plan),encoded(manifest),None,None,now,now))
            for index,assignment in enumerate(plan["assignments"],1):
                db.execute("INSERT INTO team_agent_tasks VALUES(?,?,?,?,?,?,'queued',NULL,NULL,NULL,?,NULL,NULL)",
                           (run["id"],f"agent-{index}",assignment["role"],assignment["task"],
                            encoded(assignment["skills"]),encoded(assignment["source_ids"]),now))

    def _read_tasks(self,run):
        with self.service.store.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM team_agent_tasks WHERE run_id=? ORDER BY id",(run["id"],))]

    def _task(self,run,identity,assignment,sources):
        service=self.service
        role=AGENT_BY_ID[assignment["role"]]
        selected=[copy.deepcopy(source) for source in sources if source["id"] in assignment["source_ids"]]
        try:
            with service.store.db() as db:
                db.execute("BEGIN IMMEDIATE");self._guard(db,run)
                changed=db.execute("UPDATE team_agent_tasks SET state='running',started_at=?,input_hash=? "
                                   "WHERE run_id=? AND id=? AND state='queued'",
                                   (service.clock(),fingerprint([run["goal"],assignment,selected]),run["id"],identity)).rowcount
                if not changed:raise ConflictError("This agent task already started or stopped.")
            model_skill={"test_plan":"tests.design","documentation":"docs.draft",
                         "change_proposal":"change.prepare","automation_workflow":"automation.draft"}.get(role["artifact_kind"])
            service._node(run,identity,"vega",role["label"],skill=model_skill,execution="model")
            if assignment["role"]=="patch_author":
                result=self._patch(run,identity,selected)
            else:
                skill_results=[]
                for skill in dict.fromkeys(assignment["skills"]):
                    if skill=="automation.catalog":
                        value=service._tool(run,identity+":"+skill,"Automation catalog",skill,
                            lambda:skill_catalog(run["project"]),parent=identity)
                        skill_results.append(value)
                    elif skill in SOURCE_SKILLS:
                        value=service._tool(run,identity+":"+skill,role["label"]+" source skill",skill,
                            lambda skill=skill:analyze_sources(skill,selected),parent=identity)
                        skill_results.append(value)
                    elif skill in {"files.read","workspace.read","project.snapshot","project.receipts"}:
                        kind={"project.snapshot":"project","project.receipts":"receipts"}.get(skill,"file")
                        matching=[source["id"] for source in selected if source["kind"]==kind]
                        value=service._tool(run,identity+":"+skill,"Assigned source reader",skill,
                            lambda:{"summary":f"Used {len(matching)} assigned {kind} snapshots.",
                                    "source_ids":matching,"state":"completed" if matching else "unavailable",
                                    "limitations":[] if matching else ["This assignment contains no matching source."]},
                            parent=identity)
                        skill_results.append(value)
                result=service._model(run,lambda admission:service.model.work(
                    run["goal"],assignment,selected,skill_results,admission),role=assignment["role"])
                citations=service._tool(run,identity+":citations","Citation checker","evidence.cite",
                    lambda:{"summary":"Validated the agent's source references.",
                            "citations":validate_citations(result["citations"],selected,
                                                          allowed_ids=assignment["source_ids"],max_excerpt_chars=2000)},
                    parent=identity)["citations"]
                result={**result,"citations":citations,"skill_results":skill_results}
                if assignment["role"]=="automation_designer" and result["status"]=="completed":
                    spec=workflow_spec(json.loads(result["artifact"]["body"]))
                    workflow=service._tool(run,identity+":workflow","Workflow builder","automation.draft",
                        lambda:build_workflow(run["project"],spec),parent=identity)
                    validation=service._tool(run,identity+":workflow-validation","Workflow scope checker","automation.validate",
                        lambda:validate_workflow(run["project"],spec,workflow),parent=identity)
                    result.update(workflow_spec=spec,workflow=workflow)
                    result["skill_results"].append(validation)
                    result["artifact"]["body"]=json.dumps(workflow,ensure_ascii=False,indent=2)
                result["artifact"]={**result["artifact"],"state":"draft",
                    "sha256":fingerprint(result["artifact"]),"created_by":identity}
            with service.store.db() as db:
                db.execute("BEGIN IMMEDIATE");self._guard(db,run)
                state="completed" if result["status"]=="completed" else "blocked"
                db.execute("UPDATE team_agent_tasks SET state=?,result=?,finished_at=? "
                           "WHERE run_id=? AND id=? AND state='running'",
                           (state,encoded(result),service.clock(),run["id"],identity))
            service._finish_node(run,identity,result["summary"],state=state)
            return result
        except Exception as error:
            message=str(error)[:600] if isinstance(error,(ConversationError,ConflictError,ValueError)) else "This specialist could not complete its assigned work."
            with service.store.db() as db:
                db.execute("UPDATE team_agent_tasks SET state='failed',error=?,finished_at=? "
                           "WHERE run_id=? AND id=? AND state IN ('queued','running')",
                           (message,service.clock(),run["id"],identity))
                db.execute("UPDATE team_nodes SET state='failed',summary=?,finished_at=? "
                           "WHERE run_id=? AND id=? AND state='running'",
                           (message,service.clock(),run["id"],identity))
            return None

    def _patch(self,run,identity,sources):
        service=self.service
        source=next((source for source in sources if source["id"]=="selected_file"),None)
        if source is None:
            raise ValueError("The patch author was not assigned the selected file.")
        def generate(instruction,path,text,*,admission):
            return service._model(run,lambda admit:service.brain.suggest_file_change(
                run["goal"],path,text,admission=lambda reserve:admission(lambda db:admit(reserve,db))),
                role="patch_author")
        proposal=service.tools.suggest(run["owner"],{
            **json.loads(run["binding"]),"path":run["path"],"expected_sha256":source["sha256"],
            "request_key":"team:"+run["id"],"instruction":run["goal"],
        },generate,review_run=run["id"])["proposal"]
        with service.store.db() as db:
            self._guard(db,run)
            db.execute("UPDATE team_runs SET proposal_id=? WHERE id=?",(proposal["id"],run["id"]))
        job=service.tools.jobs.get(proposal["id"])
        while job and job.is_alive():
            job.join(.2)
            with service.store.db() as db:self._guard(db,run)
        proposal=service.tools.get(run["owner"],proposal["id"])["proposal"]
        if proposal["state"]!="awaiting_approval":
            raise ValueError(proposal["receipt"]["detail"])
        with service.store.db() as db:
            row=service.tools._row(db,run["owner"],proposal["id"])
            proposed=text_from_bytes(row["proposed"])
        check=service._tool(run,identity+":syntax","Proposed syntax checker","syntax.check",
                            lambda:verify_syntax(run["path"],proposed,node=self.node),parent=identity)
        if check["state"]=="failed":
            raise ValueError("The proposed change failed syntax verification.")
        artifact={"title":"Proposed change to "+run["path"],"kind":"change_proposal",
                  "body":proposal["summary"],"state":"draft","proposal_id":proposal["id"],
                  "sha256":proposal["after_sha256"],"created_by":identity}
        return {"status":"completed","summary":proposal["summary"],"findings":[],
                "artifact":artifact,"citations":change_citations(source,proposed),
                "skill_results":[check],"proposal":{key:proposal[key] for key in (
                    "id","path","state","revision","review_hash","before_sha256","after_sha256","diff")}}

    def _review_packet(self,run,plan,tasks,checks,sources):
        agents=[]
        cited={}
        for task in tasks:
            result=json.loads(task["result"]) if task["result"] else None
            if result:
                result["skill_results"]=[{key:value.get(key) for key in ("skill","summary","state","detail","limitations","truncated")}
                                         for value in result.get("skill_results",[])]
                # Saved citations contain bounded UI previews. Reconstruct the
                # review evidence from the same authorized source snapshot.
                citations=validate_citations(
                    [{key:value[key] for key in ("source_id","start_line","end_line")}
                     for value in result["citations"]],
                    sources,allowed_ids=json.loads(task["source_ids"]),max_excerpt_chars=0)
                for saved,checked in zip(result["citations"],citations):
                    if saved["sha256"]!=checked["sha256"]:
                        raise ValueError("The cited source changed before Vega's review.")
                    cited.setdefault(checked["source_id"],[]).append(
                        (checked["start_line"],checked["end_line"]))
                result["citations"]=[{key:value[key] for key in
                    ("source_id","path","sha256","start_line","end_line")} for value in citations]
            agents.append({"id":task["id"],"role":task["role"],"assignment":task["assignment"],
                           "state":task["state"],"result":result,"error":task["error"]})
        source_evidence=[]
        for source in sources:
            if source["id"] not in cited:continue
            merged=[]
            for start,end in sorted(set(cited[source["id"]])):
                if merged and start<=merged[-1][1]+1:
                    merged[-1][1]=max(merged[-1][1],end)
                else:
                    merged.append([start,end])
            lines=source["text"].splitlines() or [""]
            source_evidence.append({
                **{key:source.get(key) for key in ("id","path","kind","sha256","truncated")},
                "line_count":len(lines),
                "ranges":[{"start_line":start,"end_line":end,
                           "numbered_text":"\n".join(f"{index}: {lines[index-1]}"
                                                    for index in range(start,end+1))}
                          for start,end in merged]})
        # Complete artifacts and complete diffs are kept. Oversized reviews
        # stop visibly rather than silently hiding a changed hunk or worker output.
        packet={"plan":{"summary":plan["summary"],"assignments":plan["assignments"]},
                "agents":agents,"source_evidence":source_evidence,
                "verification":checks,"functional_tests_run":False,
                "scope":{"project":run["project"],"file":run["path"]}}
        if len(encoded(packet))>21000:
            raise ValueError("The complete agent outputs exceed Vega's review size limit. Split this into a smaller request; no code was approved.")
        return packet

    def _settle(self,run,review,packet,evidence,checks,*,model_reviewed):
        service=self.service
        binding=fingerprint(packet)
        accepted=review["status"]=="accepted"
        tasks=self._read_tasks(run)
        if accepted and any(task["state"]!="completed" for task in tasks):
            raise ConflictError("Vega cannot accept incomplete specialist work.")
        result={"summary":review["summary"],"findings":review.get("findings",[]),
                "verification":checks,"source_verified":True,"functional_tests_run":False,
                "agent_results":[{"id":task["id"],"role":task["role"],"state":task["state"]} for task in tasks],
                "vega_review":{**review,"model_reviewed":model_reviewed,"binding":binding},
                "implementation":"read_only_skills_completed" if accepted else "review_blocked"}
        if run["mode"]=="inspect":
            result["files"]=[{key:item.get(key) for key in ("path","sha256","bytes","truncated","status","reason")}
                             for item in evidence.get("files",[])]
            result["file_reads"]=sum(item.get("status")=="read" for item in result["files"])
            result["source_verified"]=any(item.get("status") in {"read","metadata_only"} for item in result["files"])
            result["source_receipts"]=evidence.get("source_receipts",[])
            result["search"]=evidence.get("search")
            result["search_truncated"]=evidence.get("truncated",False)
        with service.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current=self._guard(db,run)
            proposal_id=current["proposal_id"]
            if proposal_id:
                proposal=service.tools._row(db,run["owner"],proposal_id)
                snapshot=next((json.loads(task["result"]).get("proposal") for task in tasks
                               if task["role"]=="patch_author" and task["result"]),None)
                if proposal["state"]=="awaiting_approval" and proposal["review_state"]=="pending":
                    if accepted and not snapshot:
                        raise ConflictError("Vega has no reviewed proposal snapshot.")
                    service.tools.record_agent_review(db,run["owner"],proposal_id,run_id=run["id"],
                        expected_revision=snapshot["revision"] if snapshot else proposal["revision"],
                        review_hash=snapshot["review_hash"] if snapshot else proposal["review_hash"],
                        binding=binding,accepted=accepted)
                elif accepted:
                    raise ConflictError("The proposed change stopped before Vega's review settled.")
                if accepted:result["implementation"]="awaiting_owner_review"
            state=("awaiting_approval" if proposal_id else "completed") if accepted else (
                "needs_revision" if review["status"]=="needs_revision" else "review_blocked")
            public_review={**review,"model_reviewed":model_reviewed,"binding":binding}
            db.execute("UPDATE team_delegations SET review=?,review_binding=?,updated_at=? WHERE run_id=?",
                       (encoded(public_review),binding,service.clock(),run["id"]))
            if accepted and run["mode"]=="automation":
                designer=next(task for task in tasks if task["role"]=="automation_designer")
                spec=json.loads(designer["result"])["workflow_spec"]
                if service.automations:
                    result["automation_draft_id"]=service.automations.record_draft(
                        db,run["owner"],run["project"],spec,source_run=run["id"],review_binding=binding)
                result["implementation"]="workflow_draft_ready"
            db.execute("UPDATE team_runs SET state=?,result=?,error=?,updated_at=? WHERE id=?",
                       (state,encoded(result),None if accepted else review["summary"][:600],
                        service.clock(),run["id"]))
        service._finish_node(run,"vega",review["summary"],state="completed" if accepted else "stopped")
        if state=="awaiting_approval":
            service._publish(run)
            if run["source"]=="telegram_owner":
                service.store.enqueue("team-review:"+run["id"],run["project"],service._approval_notice(run))
        else:
            service._deliver(run)

    def _run(self,run):
        service=self.service
        service._node(run,"vega",None,"Vega",execution="coordinator")
        service._node(run,"sources","vega","Source collection",execution="coordinator")
        service._state(run,"reading")
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix="vega-sources") as pool:
            if run["mode"] in {"report","automation"}:
                operation=lambda:{"summary":"Read registered project evidence.",
                                  "project":service.snapshot(run["owner"],run["project"])}
                skill="project.snapshot"
            elif run["mode"]=="inspect" or run["mode"]=="change" and run["path"] is None:
                operation=lambda:service._discover(run)
                skill="files.read"
            else:
                operation=lambda:service._file(run)
                skill="workspace.read"
            source_future=pool.submit(service._tool,run,"source","Source reader",skill,operation,parent="sources")
            receipt_future=pool.submit(service._tool,run,"receipts","Receipt checker","project.receipts",
                                       lambda:service._receipts(run),parent="sources")
            evidence,receipts=source_future.result(),receipt_future.result()
        service._finish_node(run,"sources","Collected the authorized source snapshot.")
        checks=[]
        candidates=None
        if run["mode"]=="change":
            if run["path"] is None:
                candidates=service._candidates(run,evidence)
                if not candidates:raise ValueError("No supported file was found for this bounded change.")
                sources=self._sources({**run,"mode":"report"},{"project":{}},receipts)[:2]
                sources.append(self._source("selected_file","A supplied candidate file","file","Selected by the plan.",truncated=True))
            else:
                file=evidence["file"]
                candidates=[{"path":run["path"],"text":file["text"][:3000],
                             "sha256":file["sha256"],"excerpt_truncated":len(file["text"])>3000}]
                sources=self._sources(run,evidence,receipts)
        else:
            sources=self._sources(run,evidence,receipts)
        service._state(run,"planning")
        service._node(run,"planner","vega","Vega planner",execution="model")
        plan=service._model(run,lambda admission:service.model.delegate_plan(
            run["goal"],run["mode"],sources,admission,candidates=candidates),role="planner")
        service._finish_node(run,"planner",plan["summary"])
        if run["mode"]=="change":
            selected=plan["selected_path"]
            candidate=next((item for item in candidates if item["path"]==selected),None)
            if candidate is None or run["path"] is not None and selected!=run["path"]:
                raise ValueError("Vega did not select the exact authorized change file.")
            with service.store.db() as db:
                self._guard(db,run)
                db.execute("UPDATE team_runs SET path=? WHERE id=?",(selected,run["id"]))
            run["path"]=selected
            evidence=service._file(run)
            if candidate.get("sha256") and candidate["sha256"]!=evidence["file"]["sha256"]:
                raise ConflictError("The selected source changed during planning.")
            sources=self._sources(run,evidence,receipts)
            if any("selected_file" not in assignment["source_ids"] for assignment in plan["assignments"]):
                raise ValueError("Both change specialists must receive the selected file.")
        if run["mode"] in {"review","change"}:
            checks.append(verify_syntax(run["path"],evidence["file"]["text"],node=self.node))
        self._save_plan(run,plan,sources)
        service._state(run,"delegating")
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix="vega-specialist") as pool:
            futures=[pool.submit(self._task,run,f"agent-{index}",assignment,sources)
                     for index,assignment in enumerate(plan["assignments"],1)]
            for future in futures:future.result()
        tasks=self._read_tasks(run)
        service._state(run,"reviewing")
        packet=self._review_packet(run,plan,tasks,checks,sources)
        if any(task["state"]!="completed" for task in tasks):
            reasons=[]
            for task in tasks:
                if task["state"]=="completed":continue
                result=json.loads(task["result"]) if task["result"] else {}
                reasons.append(task["error"] or result.get("summary") or "Provide the missing source information.")
            self._settle(run,{"status":"blocked","summary":"A specialist did not complete its assigned work. No agent change was approved.",
                              "findings":[],"required_changes":reasons},
                         packet,evidence,checks,model_reviewed=False)
            return
        service._node(run,"vega-review","vega","Vega review",execution="model")
        review=service._model(run,lambda admission:service.model.review_delegation(
            run["goal"],packet,admission),role="vega_reviewer")
        status=review["verdict"]
        if status=="accepted" and review["required_changes"]:
            status="needs_revision"
        service._finish_node(run,"vega-review",review["summary"],
                             state="completed" if status=="accepted" else "needs_revision")
        self._settle(run,{"status":status,"summary":review["summary"],"findings":review["findings"],
                          "required_changes":review["required_changes"],"usage":review.get("_usage")},
                     packet,evidence,checks,model_reviewed=True)

    def process(self,run):
        service=self.service
        try:
            self._run(run)
        except Exception as error:
            message=str(error)[:600] if isinstance(error,(ConversationError,ConflictError,ValueError)) else "The delegated work could not complete safely."
            with service.store.db() as db:
                current=service._row(db,run["owner"],run["id"])
                if current["state"] not in SETTLED:
                    planned=db.execute("SELECT 1 FROM team_delegations WHERE run_id=?",(run["id"],)).fetchone()
                    state="expired" if service.clock()>current["expires_at"] else "review_blocked" if planned else "failed"
                    db.execute("UPDATE team_runs SET state=?,error=?,updated_at=? WHERE id=?",
                               (state,message,service.clock(),run["id"]))
                db.execute("UPDATE team_agent_tasks SET state='interrupted',finished_at=? "
                           "WHERE run_id=? AND state IN ('queued','running')",(service.clock(),run["id"]))
                db.execute("UPDATE team_nodes SET state='stopped',finished_at=? "
                           "WHERE run_id=? AND state='running'",(service.clock(),run["id"]))
                db.execute("UPDATE team_delegations SET review=?,updated_at=? WHERE run_id=? AND review IS NULL",
                           (encoded({"status":"blocked","summary":message,"model_reviewed":False,
                                     "required_changes":[]}),service.clock(),run["id"]))
                proposal_id=current["proposal_id"]
            if proposal_id:
                try:
                    proposal=service.tools.get(run["owner"],proposal_id)["proposal"]
                    if proposal["can_cancel"]:
                        service.tools.cancel(run["owner"],proposal_id,{"expected_revision":proposal["revision"]})
                except (ValueError,RuntimeError,OSError):
                    pass
            if current["state"] not in {"cancelled","interrupted"}:
                service._deliver(run)
        return True

"""Durable owner-scoped Vega delegation, using implemented skills and receipts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
import uuid

from companion import ConflictError
from memory_policy import contains_secret
from project_files import relative_path, text_from_bytes
from project_requests import ProjectRequests
from routing_targets import dispatch_target
from team_model import TeamModel
from team_skills import SKILLS, MODES, verify_syntax
from work_activity import agent_activity, request_activity
from agent_skills import AGENTS, AGENT_BY_ID
from agent_delegation import DelegationRunner
import project_checks

TERMINAL={"completed","failed","cancelled","interrupted","expired","needs_revision","review_blocked"}
BUSY={"planning","reading","delegating","preparing","reviewing","verifying","applying"}
MAX_REQUESTS=4
MAX_PARALLEL_MODELS=2
LIFETIME=30*60
READ_ONLY_MODES={"report","inspect","review"}
_NODE_PATH = shutil.which("node")
NODE = Path(_NODE_PATH) if _NODE_PATH else None


class TeamService:
    def __init__(self,store,tasks,brain,project_tools,snapshot,*,responses=None,
                 voice_guard=None,clock=time.time,model=None,recover=True,settings=None,
                 files=None,publish=None,project_requests=None,automations=None):
        self.store,self.tasks,self.brain,self.tools,self.snapshot=store,tasks,brain,project_tools,snapshot
        self.responses,self.voice_guard,self.clock=responses,voice_guard,clock
        self.model=model or TeamModel(brain)
        self.settings=settings or brain.settings
        self.files,self.publish=files,publish
        self.project_requests=project_requests
        self.automations=automations
        self.delegation=DelegationRunner(self,node=NODE)
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS team_runs (
                    id TEXT PRIMARY KEY,owner INTEGER NOT NULL,client_key TEXT NOT NULL,
                    source TEXT NOT NULL,origin_update_id INTEGER,project TEXT NOT NULL,
                    thread_id TEXT NOT NULL,mode TEXT NOT NULL,goal TEXT NOT NULL,path TEXT,
                    binding TEXT NOT NULL,preference_revision INTEGER NOT NULL,context_revision INTEGER NOT NULL,
                    state TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,model_calls INTEGER NOT NULL DEFAULT 0,
                    provider_active INTEGER NOT NULL DEFAULT 0,proposal_id TEXT,result TEXT,error TEXT,
                    UNIQUE(owner,client_key)
                );
                CREATE TABLE IF NOT EXISTS team_nodes (
                    run_id TEXT NOT NULL,id TEXT NOT NULL,parent_id TEXT,role TEXT NOT NULL,
                    skill TEXT,execution TEXT NOT NULL,state TEXT NOT NULL,summary TEXT,
                    started_at REAL,finished_at REAL,PRIMARY KEY(run_id,id)
                );
                CREATE TABLE IF NOT EXISTS team_delegations (
                    run_id TEXT PRIMARY KEY,plan TEXT NOT NULL,source_manifest TEXT NOT NULL,
                    review TEXT,review_binding TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS team_agent_tasks (
                    run_id TEXT NOT NULL,id TEXT NOT NULL,role TEXT NOT NULL,assignment TEXT NOT NULL,
                    skills TEXT NOT NULL,source_ids TEXT NOT NULL,state TEXT NOT NULL,
                    input_hash TEXT,result TEXT,error TEXT,created_at REAL NOT NULL,
                    started_at REAL,finished_at REAL,PRIMARY KEY(run_id,id)
                );
                CREATE TABLE IF NOT EXISTS team_model_leases (
                    id TEXT PRIMARY KEY,run_id TEXT NOT NULL,role TEXT NOT NULL,
                    active INTEGER NOT NULL,admitted_at REAL NOT NULL,released_at REAL
                );
            """)
            if recover:
                db.execute("UPDATE team_runs SET state='interrupted',provider_active=0,error=?,updated_at=? "
                           "WHERE state IN ('planning','reading','delegating','preparing','reviewing','verifying','applying')",
                           ("Vega stopped during this run. Work was not replayed.",clock()))
                db.execute("UPDATE team_nodes SET state='interrupted',finished_at=? WHERE state='running'",(clock(),))
                db.execute("UPDATE team_agent_tasks SET state='interrupted',finished_at=? "
                           "WHERE state IN ('queued','running')",(clock(),))
                db.execute("UPDATE team_model_leases SET active=0,released_at=? WHERE active=1",(clock(),))

    def _model_policy(self):
        value={key:self.settings.get(key) for key in (
            "model","provider","region","reasoning_effort","max_output_tokens",
            "max_conversations_per_hour","request_timeout_seconds")}
        return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()

    def _principal(self,db,owner,*,enabled=True):
        revision=ProjectRequests._principal(db,owner)
        pref=db.execute("SELECT payload FROM companion_preferences WHERE owner=?",(owner,)).fetchone()
        if enabled and pref and not json.loads(pref["payload"]).get("project_context_enabled",True):
            raise PermissionError("Project context is disabled.")
        context=db.execute("SELECT revision,payload FROM companion_context WHERE owner=?",(owner,)).fetchone()
        return revision,context["revision"] if context else 0,json.loads(context["payload"]) if context else {}

    def _row(self,db,owner,identity):
        row=db.execute("SELECT * FROM team_runs WHERE owner=? AND id=?",(owner,identity)).fetchone()
        if not row:raise ValueError("This work request is unavailable.")
        return row

    @staticmethod
    def _pause(db):
        values={row["key"]:json.loads(row["value"]) for row in db.execute(
            "SELECT key,value FROM settings WHERE key IN ('work_paused','work_pause_revision')")}
        return bool(values.get("work_paused",False)),values.get("work_pause_revision",0)

    @staticmethod
    def _can_start_once(row):
        # A fixed-file change may be prepared once. Applying its exact diff
        # remains a separate, revision-bound owner operation.
        return row["mode"] in READ_ONLY_MODES or (row["mode"] == "change" and bool(row["path"]))

    @staticmethod
    def _started_once(row,revision):
        binding=json.loads(row["binding"])
        granted=binding.get("manual_start_revision")
        return TeamService._can_start_once(row) and type(granted) is int and granted==revision

    def _guard(self,db,owner,identity,states,*,allow_paused=False):
        row=self._row(db,owner,identity)
        pref,context,view=self._principal(db,owner)
        if row["state"] not in states:raise ConflictError("This work request stopped or changed state.")
        if self.clock()>row["expires_at"]:raise ConflictError("This work request expired.")
        binding=json.loads(row["binding"])
        if binding.get("model_policy") and binding["model_policy"]!=self._model_policy():
            raise ConflictError("The selected model policy changed during this work.")
        if pref!=row["preference_revision"] or (
            binding.get("context_policy")!="automatic" and context!=row["context_revision"]
        ):
            raise ConflictError("Owner preferences changed.")
        if dispatch_target(self.tasks(),row["project"])["thread_id"]!=row["thread_id"]:
            raise ConflictError("The registered project changed.")
        paused,pause_revision=self._pause(db)
        if paused and not allow_paused and not self._started_once(row,pause_revision):
            raise ConflictError("Project work is paused.")
        if row["mode"] in {"review","change"}:
            self.tools._workspace(db,owner,binding,writing=row["mode"]=="change")
        if binding.get("file_access"):
            if not self.files:raise ConflictError("Local file access is unavailable.")
            self.files.validate(owner,binding["file_access"])
        if row["origin_update_id"] is not None:
            origin=db.execute("SELECT * FROM inbox WHERE owner_id=? AND update_id=?",(owner,row["origin_update_id"])).fetchone()
            if not origin or origin["state"] in {"cancelled","interrupted","expired","awaiting_clarification"}:
                raise ConflictError("The original owner request is no longer available.")
            context_row=db.execute("SELECT payload FROM inbox_context WHERE update_id=?",(row["origin_update_id"],)).fetchone()
            provenance=json.loads(context_row["payload"]) if context_row else {}
            if provenance.get("origin")=="speaker_matched" and (not self.voice_guard or not self.voice_guard(provenance,owner)):
                raise ConflictError("The voice source changed during the run.")
        return row

    def create(self,owner,payload,*,source="local_owner_ui",origin_update_id=None):
        if source not in {"local_owner_ui","telegram_owner","telegram_miniapp","codex_owner_request"}:
            raise ValueError("An explicit owner request is required.")
        if not isinstance(payload,dict) or set(payload)-{"request_key","project","mode","goal","path","file_scope"}:
            raise ValueError("Use the supported work request fields.")
        key,project,mode,goal=map(payload.get,("request_key","project","mode","goal"))
        if not isinstance(key,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}",key):
            raise ValueError("Use a stable request identity.")
        if project not in self.tasks() or mode not in MODES:
            raise ValueError("A registered project and supported task are required.")
        file_scope=payload.get("file_scope","project")
        if file_scope not in {"project","local"} or file_scope=="local" and mode!="inspect":
            raise ValueError("Local discovery grants reads only.")
        if not isinstance(goal,str) or not 1<=len(goal.strip())<=2000 or contains_secret(goal):
            raise ValueError("Use a non-secret task description of at most2,000 characters.")
        path=payload.get("path") if mode in {"inspect","change"} else (
            relative_path(payload.get("path")) if mode=="review" else None)
        if path is not None and (not isinstance(path,str) or len(path)>1000):
            raise ValueError("Use a valid file path.")
        read_binding=None
        if mode=="inspect" or mode=="change" and path is None:
            if not self.files:raise ValueError("Automatic file discovery is unavailable.")
            read_binding=self.files.bind(owner,None if file_scope=="local" else project,goal,path)
        identity,now=str(uuid.uuid4()),self.clock()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            pref,context,view=self._principal(db,owner)
            previous=db.execute("SELECT * FROM team_runs WHERE owner=? AND client_key=?",(owner,key)).fetchone()
            if previous:
                prior=json.loads(previous["binding"])
                if ((previous["project"],previous["mode"],previous["goal"],prior.get("requested_path",previous["path"]),prior.get("file_scope","project"))
                        !=(project,mode,goal,path,file_scope)):
                    raise ConflictError("This request identity already belongs to different work.")
                return self._public(db,previous)
            pending=db.execute("SELECT count(*) FROM team_runs WHERE owner=? AND state NOT IN "
                               "('completed','failed','cancelled','interrupted','expired','needs_revision','review_blocked')",(owner,)).fetchone()[0]
            if pending>=8:raise ValueError("Finish or cancel an existing task before adding another.")
            target=dispatch_target(self.tasks(),project)
            binding={"project":project,"thread_id":target["thread_id"],"context_policy":"automatic",
                     "requested_path":path,"file_scope":file_scope,
                     "delegation_version":1,"model_policy":self._model_policy()}
            if read_binding:binding["file_access"]=read_binding
            if mode in {"review","change"}:
                workspace=db.execute("SELECT * FROM project_tool_workspaces WHERE owner=? AND project=?",(owner,project)).fetchone()
                if not workspace:raise ValueError("This project has no registered workspace for prepared code changes.")
                binding["workspace_revision"]=workspace["revision"]
                _,files=self.tools._workspace(db,owner,binding,writing=mode=="change")
                if path is not None:
                    if Path(path).is_absolute():
                        try:path=Path(path).relative_to(Path(workspace["root"])).as_posix()
                        except ValueError:raise PermissionError("This change is outside the project's reviewed write scope.") from None
                    path=relative_path(path)
                    current=files.read(path)
                    if mode=="change" and not self.tools._writable(path,workspace):
                        raise PermissionError("This control file cannot be changed through a prepared edit.")
                    if mode=="change" and len(current["text"])>12_000:
                        raise ValueError("This file exceeds the bounded change size; its project route remains available.")
            if origin_update_id is not None:
                origin=db.execute("SELECT owner_id FROM inbox WHERE update_id=?",(origin_update_id,)).fetchone()
                provenance=db.execute("SELECT payload FROM inbox_context WHERE update_id=?",(origin_update_id,)).fetchone()
                data=json.loads(provenance["payload"]) if provenance else {}
                if not origin or origin["owner_id"]!=owner or data.get("origin") not in {"typed","telegram_text","manual_review","speaker_matched"}:
                    raise PermissionError("The original task is not a confirmed owner instruction.")
                if data.get("origin")=="speaker_matched" and (not self.voice_guard or not self.voice_guard(data,owner)):
                    raise PermissionError("The voice task source is no longer valid.")
            db.execute("INSERT INTO team_runs(id,owner,client_key,source,origin_update_id,project,thread_id,mode,goal,path,"
                       "binding,preference_revision,context_revision,state,created_at,updated_at,expires_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?)",
                       (identity,owner,key,source,origin_update_id,project,target["thread_id"],mode,goal,path,
                        json.dumps(binding),pref,context,now,now,now+LIFETIME))
            return self._public(db,self._row(db,owner,identity))

    def start_once(self,owner,identity,payload):
        if payload!={}:raise ValueError("Run once takes no task changes or approval fields.")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._principal(db,owner)
            row=self._row(db,owner,identity)
            binding=json.loads(row["binding"])
            _,revision=self._pause(db)
            if "manual_start_revision" in binding:
                if not self._started_once(row,revision):
                    raise ConflictError("The earlier Run once was stopped. Create a new task to start again.")
                return self._public(db,row)
            if not self._can_start_once(row) or binding.get("delegation_version")!=1:
                raise ValueError("Run once needs a read-only task or a change to one selected file.")
            self._guard(db,owner,identity,{"queued"},allow_paused=True)
            binding.update(manual_start_revision=revision,manual_started_at=self.clock())
            db.execute("UPDATE team_runs SET binding=?,updated_at=? WHERE id=?",
                       (json.dumps(binding),self.clock(),identity))
            return self._public(db,self._row(db,owner,identity))

    def _public(self,db,row):
        nodes=[dict(x) for x in db.execute("SELECT id,parent_id,role,skill,execution,state,summary,started_at,finished_at "
                                          "FROM team_nodes WHERE run_id=? ORDER BY rowid",(row["id"],))]
        proposal=None
        if row["proposal_id"]:
            value=db.execute("SELECT * FROM project_tool_proposals WHERE owner=? AND id=?",(row["owner"],row["proposal_id"])).fetchone()
            if value:proposal=self.tools._public(value,delegation=row["id"])
        delegation=db.execute("SELECT * FROM team_delegations WHERE run_id=?",(row["id"],)).fetchone()
        review=json.loads(delegation["review"]) if delegation and delegation["review"] else None
        agents=[{"id":task["id"],"role":task["role"],
                 "label":AGENT_BY_ID.get(task["role"],{}).get("label",task["role"]),
                 "task":task["assignment"],"skills":json.loads(task["skills"]),
                 "source_ids":json.loads(task["source_ids"]),"state":task["state"],
                 "result":json.loads(task["result"]) if task["result"] else None,
                 "model_requests":db.execute("SELECT count(*) FROM team_model_leases WHERE run_id=? AND role=?",
                                             (row["id"],task["role"])).fetchone()[0],
                 "error":task["error"],"started_at":task["started_at"],"finished_at":task["finished_at"]}
                for task in db.execute("SELECT * FROM team_agent_tasks WHERE run_id=? ORDER BY id",(row["id"],))]
        if proposal and json.loads(row["binding"]).get("delegation_version")==1:
            accepted=bool(review and review.get("status")=="accepted"
                          and delegation["review_binding"]==value["review_binding"])
            if not accepted:
                proposal["can_apply"]=False
            if proposal["can_apply"]:
                try:self._guard(db,row["owner"],row["id"],{"awaiting_approval"})
                except (ValueError,RuntimeError,OSError):proposal["can_apply"]=False
        responses=[]
        if row["origin_update_id"] is not None and self.responses:
            from project_responses import ProjectResponses
            responses=[ProjectResponses._public(db,x) for x in db.execute(
                "SELECT * FROM project_report_responses WHERE owner=? AND origin_update_id=?",
                (row["owner"],row["origin_update_id"]))]
        public={
            "id":row["id"],"request_key":row["client_key"],"project":row["project"],"label":self.tasks().get(row["project"],{}).get("label",row["project"]),
            "origin_update_id":str(row["origin_update_id"]) if row["origin_update_id"] is not None else None,
            "mode":row["mode"],"goal":row["goal"],"path":row["path"],"state":row["state"],
            "created_at":row["created_at"],"updated_at":row["updated_at"],"expires_at":row["expires_at"],
            "usage":{"model_requests":row["model_calls"],"max_requests":MAX_REQUESTS,
                     "active_model_requests":row["provider_active"],"parallel_model_limit":MAX_PARALLEL_MODELS},
            "agents":agents,"vega_review":review,
            "delegation_version":json.loads(row["binding"]).get("delegation_version",0),
            "nodes":nodes,"result":json.loads(row["result"]) if row["result"] else None,
            "error":row["error"],"proposal_id":row["proposal_id"],"proposal":proposal,"responses":responses,
            "can_cancel":row["state"] not in TERMINAL|{"applying"},
            "can_start":row["state"]=="queued" and self._can_start_once(row)
                and self.clock()<=row["expires_at"] and json.loads(row["binding"]).get("delegation_version")==1
                and "manual_start_revision" not in json.loads(row["binding"]),
            "manual_started_at":json.loads(row["binding"]).get("manual_started_at"),
            "model":self.settings["model"],
        }
        public["activity"]=agent_activity(public)
        workspace=db.execute("SELECT root FROM project_tool_workspaces WHERE owner=? AND project=?",
                             (row["owner"],row["project"])).fetchone()
        recipe=project_checks.recipe_for(row["project"],row["path"],workspace["root"]) if workspace else None
        public["check_recipe"]=recipe if (row["state"] in {"completed","failed"} and proposal
                                          and proposal["state"]=="applied") else None
        result=public["result"] or {}
        public["checks_current"]=bool(recipe and project_checks.is_current(
            result.get("functional_checks"),result.get("applied_sha256"),
            recipe_id=recipe["id"],path=row["path"]))
        if (result.get("functional_checks") or {}).get("state")=="passed" and not public["checks_current"]:
            result["summary"]="Change applied. Previous checks need a fresh run."
        return public

    def get(self,owner,identity):
        with self.store.db() as db:
            self._principal(db,owner)
            return self._public(db,self._row(db,owner,identity))

    def catalog(self,owner,project=None,*,include_file_access=True):
        projects=self.tools.projects(owner)["projects"]
        if project is not None:
            if not isinstance(project,str) or project not in self.tasks():
                raise ValueError("Choose a registered project.")
            projects=[item for item in projects if item["alias"]==project]
        with self.store.db() as db:
            self._principal(db,owner)
            condition="owner=?" if project is None else "owner=? AND project=?"
            parameters=(owner,) if project is None else (owner,project)
            rows=db.execute("SELECT * FROM team_runs WHERE "+condition+
                            " ORDER BY created_at DESC LIMIT 30",parameters).fetchall()
            runs=[self._public(db,row) for row in rows]
        requests=self.project_requests.list(owner,project=project,limit=30) if self.project_requests else []
        for request in requests:
            request["activity"]=request_activity(request)
        with self.store.db() as db:
            self._principal(db,owner)
        result={"runs":runs,"skills":SKILLS,"projects":projects,
                "agents":AGENTS,"delegation":{"version":1,"parallel_model_workers":MAX_PARALLEL_MODELS,
                                             "max_model_requests":MAX_REQUESTS},
                "requests":requests,"work_paused":bool(self.store.get("work_paused",False)),
                "snapshot_at":self.clock(),"work_activity_version":1,
                "model":{key:self.settings.get(key) for key in ("model","provider","region","max_output_tokens","max_conversations_per_hour")}}
        activity = getattr(self, "codex_activity", None)
        if activity is not None:
            result["codex_activity"] = activity.view(owner, project)
        project_activity = getattr(self, "project_activity", None)
        if project_activity is not None:
            views = project_activity.views(owner)
            result["project_activity"] = {item["alias"]: {
                **views.get(item["alias"], {"available": False, "label": "Chat not connected"}),
                "label_project": item.get("label", item["alias"]),
            } for item in projects}
        # File capabilities
        # require a fresh filesystem inspection when they are actually needed.
        if include_file_access:
            result["file_access"]=self.files.capabilities(owner) if self.files else {"available":False}
        return result

    def _node(self,run,identity,parent,role,skill=None,execution="tool"):
        with self.store.db() as db:
            self._guard(db,run["owner"],run["id"],BUSY)
            db.execute("INSERT OR IGNORE INTO team_nodes VALUES(?,?,?,?,?,?,'running',NULL,?,NULL)",
                       (run["id"],identity,parent,role,skill,execution,self.clock()))

    def _finish_node(self,run,identity,summary,state="completed"):
        with self.store.db() as db:
            db.execute("UPDATE team_nodes SET state=?,summary=?,finished_at=? WHERE run_id=? AND id=?",
                       (state,summary[:2000],self.clock(),run["id"],identity))

    def _state(self,run,state):
        with self.store.db() as db:
            self._guard(db,run["owner"],run["id"],BUSY|{"queued"})
            db.execute("UPDATE team_runs SET state=?,updated_at=? WHERE id=?",(state,self.clock(),run["id"]))

    def _admission(self,run,reserve,db=None,*,lease_id,role):
        def admit(connection):
            row=self._guard(connection,run["owner"],run["id"],BUSY)
            if row["model_calls"]>=MAX_REQUESTS:raise ConflictError("This run reached its bounded model-call allowance.")
            if row["provider_active"]>=MAX_PARALLEL_MODELS:raise ConflictError("This run reached its parallel model limit.")
            if connection.execute("SELECT 1 FROM team_model_leases WHERE id=?",(lease_id,)).fetchone():
                raise ConflictError("This model request was already admitted.")
            now=reserve(connection)
            connection.execute("INSERT INTO team_model_leases VALUES(?,?,?,1,?,NULL)",
                               (lease_id,run["id"],role,now))
            connection.execute("UPDATE team_runs SET model_calls=model_calls+1,provider_active=provider_active+1,updated_at=? WHERE id=?",
                               (self.clock(),run["id"]))
            return now
        if db is not None:return admit(db)
        with self.store.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return admit(connection)

    def _model(self,run,operation,*,role="model"):
        lease_id=uuid.uuid4().hex
        try:
            value=operation(lambda reserve,db=None:self._admission(
                run,reserve,db,lease_id=lease_id,role=role))
            with self.store.db() as db:
                self._guard(db,run["owner"],run["id"],BUSY)
                if not db.execute("SELECT 1 FROM team_model_leases WHERE id=? AND run_id=? AND active=1",
                                  (lease_id,run["id"])).fetchone():
                    raise ConflictError("The model result has no valid admission receipt.")
            return value
        finally:
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                released=db.execute("UPDATE team_model_leases SET active=0,released_at=? "
                                    "WHERE id=? AND run_id=? AND active=1",
                                    (self.clock(),lease_id,run["id"])).rowcount
                if released:
                    db.execute("UPDATE team_runs SET provider_active=MAX(0,provider_active-1) WHERE id=?",(run["id"],))

    def _tool(self,run,identity,role,skill,operation,*,parent="specialist"):
        self._node(run,identity,parent,role,skill)
        try:
            value=operation()
            with self.store.db() as db:self._guard(db,run["owner"],run["id"],BUSY)
            self._finish_node(run,identity,value.get("summary") or value.get("detail") or "Registered source read completed.")
            return value
        except Exception:
            self._finish_node(run,identity,"The skill did not complete.","failed")
            raise

    def _file(self,run):
        result=self.tools.read(run["owner"],{**json.loads(run["binding"]),"path":run["path"]})
        file=result["file"]
        return {"summary":f"Read {file['bytes']} bytes from {file['path']}.","file":file,"receipt":result["receipt"]}

    def _receipts(self,run):
        with self.store.db() as db:
            self._guard(db,run["owner"],run["id"],BUSY)
            rows=[dict(x) for x in db.execute(
                "SELECT o.event_id,o.state,o.message_id,o.sent_at,o.error FROM outbox o "
                "LEFT JOIN outbound_text t ON t.event_id=o.event_id "
                "LEFT JOIN outbound_documents d ON d.event_id=o.event_id "
                "WHERE o.task=? AND COALESCE(t.owner,d.owner)=? ORDER BY o.created_at DESC LIMIT 8",
                (run["project"],run["owner"]))]
        return {"summary":f"Read {len(rows)} saved delivery receipts.","receipts":rows}

    def _discover(self,run):
        binding=json.loads(run["binding"])
        return self.files.inspect_request(run["owner"],binding["file_access"],run["goal"])

    def _candidates(self,run,evidence):
        """Only actual files in this project's existing write scope can be selected."""
        with self.store.db() as db:
            workspace,files=self.tools._workspace(db,run["owner"],json.loads(run["binding"]),writing=True)
        candidates=[]
        for item in evidence.get("files",[]):
            try:
                value=Path(item["path"])
                path=value.relative_to(Path(workspace["root"])).as_posix() if value.is_absolute() else str(value)
                path=relative_path(path)
                if not self.tools._writable(path,workspace):continue
                current=files.read(path)
                if len(current["text"])>12000:continue
                candidates.append({"path":path,"text":current["text"][:3000],"sha256":current["sha256"],
                                   "excerpt_truncated":len(current["text"])>3000})
            except (KeyError,ValueError,OSError):
                continue
        return candidates[:3]

    def process_one(self):
        with self.store.db() as db:
            paused,pause_revision=self._pause(db)
            row=db.execute("SELECT * FROM team_runs WHERE state='queued' AND "
                           "(origin_update_id IS NULL OR EXISTS(SELECT 1 FROM inbox i "
                           "WHERE i.update_id=team_runs.origin_update_id AND i.state='conversation_done')) "
                           "AND (?=0 OR ((mode IN ('report','inspect','review') OR (mode='change' AND path IS NOT NULL AND path!='')) "
                           "AND json_extract(binding,'$.manual_start_revision')=?)) "
                           "ORDER BY created_at LIMIT 1",(int(paused),pause_revision)).fetchone()
            if not row:return False
            db.execute("BEGIN IMMEDIATE")
            paused,pause_revision=self._pause(db)
            row=self._row(db,row["owner"],row["id"])
            if paused and not self._started_once(row,pause_revision):return False
            if not db.execute("UPDATE team_runs SET state='planning',updated_at=? WHERE id=? AND state='queued'",
                              (self.clock(),row["id"])).rowcount:return False
            run=dict(row)
        if json.loads(run["binding"]).get("delegation_version")==1:
            return self.delegation.process(run)
        try:
            self._node(run,"coordinator",None,"Vega planner",execution="model")
            discovered=None
            if run["mode"]=="change" and run["path"] is None:
                self._node(run,"specialist","coordinator",MODES[run["mode"]]["specialist"],execution="coordinator")
                discovered=self._tool(run,"discovery","File discovery","files.discover",lambda:self._discover(run))
                candidates=self._candidates(run,discovered)
                if not candidates:
                    raise ValueError("I could not identify a supported file for this change in the bounded search. Name the file or use the existing project route for broader implementation.")
                plan=self._model(run,lambda admission:self.model.plan(
                    run["goal"],run["mode"],MODES[run["mode"]],admission,candidates=candidates))
                path=plan.get("selected_path")
                if path not in {item["path"] for item in candidates}:
                    raise ValueError("The available files do not identify a clear single-file change. No edit was prepared.")
                with self.store.db() as db:
                    self._guard(db,run["owner"],run["id"],BUSY)
                    db.execute("UPDATE team_runs SET path=? WHERE id=?",(path,run["id"]))
                run["path"]=path
            else:
                plan=self._model(run,lambda admission:self.model.plan(run["goal"],run["mode"],MODES[run["mode"]],admission))
            self._finish_node(run,"coordinator",plan["summary"])
            self._node(run,"specialist","coordinator",MODES[run["mode"]]["specialist"],execution="coordinator")
            self._state(run,"reading")
            with ThreadPoolExecutor(max_workers=2,thread_name_prefix="vega-subagent") as pool:
                if run["mode"]=="report":
                    first=pool.submit(self._tool,run,"source","Project source reader","project.snapshot",
                                      lambda:{"summary":"Read current registered project data.",
                                              "project":self.snapshot(run["owner"],run["project"])})
                elif run["mode"]=="inspect":
                    first=pool.submit(self._tool,run,"source","File research","files.read",lambda:self._discover(run))
                else:
                    first=pool.submit(self._tool,run,"source","Selected-file reader","workspace.read",lambda:self._file(run))
                second=pool.submit(self._tool,run,"receipts","Receipt checker","project.receipts",lambda:self._receipts(run))
                evidence,receipts=first.result(),second.result()
            evidence["delivery_evidence"]=receipts
            checks=[]
            proposal=None
            if run["mode"] in {"review","change"}:
                file=evidence["file"]
                checks.append(verify_syntax(run["path"],file["text"],node=NODE if NODE.exists() else None))
                evidence["file"]={**file,"text":file["text"][:12000],"truncated":len(file["text"])>12000}
            if run["mode"]=="change":
                self._state(run,"preparing")
                self._node(run,"editor","specialist","Patch specialist","change.prepare",execution="model")
                def generate(instruction,path,text,*,admission):
                    return self._model(run,lambda admit:self.brain.suggest_file_change(
                        instruction,path,text,admission=lambda reserve:admission(lambda db:admit(reserve,db))),
                        role="legacy_patch")
                proposal=self.tools.suggest(run["owner"],{
                    **json.loads(run["binding"]),"path":run["path"],"expected_sha256":evidence["file"]["sha256"],
                    "request_key":"team:"+run["id"],"instruction":run["goal"],
                },generate)["proposal"]
                with self.store.db() as db:
                    db.execute("UPDATE team_runs SET proposal_id=? WHERE id=?",(proposal["id"],run["id"]))
                job=self.tools.jobs.get(proposal["id"])
                while job and job.is_alive():
                    job.join(.25)
                    with self.store.db() as db:self._guard(db,run["owner"],run["id"],BUSY)
                proposal=self.tools.get(run["owner"],proposal["id"])["proposal"]
                if proposal["state"]!="awaiting_approval":raise ValueError(proposal["receipt"]["detail"])
                with self.store.db() as db:
                    raw=db.execute("SELECT proposed FROM project_tool_proposals WHERE id=?",(proposal["id"],)).fetchone()
                checks.append(verify_syntax(run["path"],text_from_bytes(raw["proposed"]),node=NODE if NODE.exists() else None))
                evidence["proposal"]={key:proposal[key] for key in ("path","summary","diff","before_sha256","after_sha256","state")}
                self._finish_node(run,"editor","Prepared a concrete diff. Owner approval is required.")
            self._state(run,"reviewing")
            self._node(run,"reviewer","coordinator","Independent reviewer",execution="model")
            evidence["verification"]=checks
            evidence["plan"]=plan
            inspection=evidence if run["mode"]=="inspect" else None
            bounded=json.dumps(evidence,ensure_ascii=False)
            if len(bounded)>23000:
                evidence={"verification":checks,"source_excerpt":bounded[:18000],
                          "source_excerpt_truncated":True}
            review=self._model(run,lambda admission:self.model.review(run["goal"],evidence,admission))
            self._finish_node(run,"reviewer",review["summary"])
            self._finish_node(run,"specialist","Delegated skills completed; actual evidence is attached.")
            result={"summary":review["summary"],"findings":review["findings"],"verification":checks,
                    "source_verified":True,"implementation":"awaiting_owner_review" if proposal else "read_only_skills_completed",
                    "functional_tests_run":False,
                    "model_usage":{"planner":plan.get("_usage"),"reviewer":review.get("_usage"),
                                   "note":"Provider input, cached-input subset and output fields are retained as reported; cached input is not added to input totals."}}
            if inspection is not None:
                result["files"]=[{key:item.get(key) for key in ("path","sha256","bytes","truncated","status","reason")}
                                 for item in inspection.get("files",[])]
                result["file_reads"]=sum(item.get("status")=="read" for item in result["files"])
                result["source_verified"]=any(item.get("status") in {"read","metadata_only"} for item in result["files"])
                result["source_receipts"]=inspection.get("source_receipts",[])
                result["search"]=inspection.get("search")
                result["search_truncated"]=inspection.get("truncated",False)
            if discovered:
                result["discovery"]={"selected_path":run["path"],"truncated":discovered.get("truncated",False)}
            if any(c["state"]=="failed" for c in checks[-1:] if proposal):
                raise ValueError("The proposed source failed syntax verification; it is not ready to apply.")
            with self.store.db() as db:
                self._guard(db,run["owner"],run["id"],BUSY)
                db.execute("UPDATE team_runs SET state=?,result=?,updated_at=? WHERE id=?",
                           ("awaiting_approval" if proposal else "completed",json.dumps(result),self.clock(),run["id"]))
            if not proposal:self._deliver(run)
            else:
                self._publish(run)
                if run["source"]=="telegram_owner":
                    self.store.enqueue("team-review:"+run["id"],run["project"],self._approval_notice(run))
        except Exception as error:
            with self.store.db() as db:
                current=self._row(db,run["owner"],run["id"])
                if current["state"] not in TERMINAL:
                    db.execute("UPDATE team_runs SET state='failed',error=?,updated_at=? WHERE id=?",
                               (str(error)[:600],self.clock(),run["id"]))
                db.execute("UPDATE team_nodes SET state='stopped',finished_at=? WHERE run_id=? AND state='running'",
                           (self.clock(),run["id"]))
            if current["proposal_id"]:
                try:
                    value=self.tools.get(run["owner"],current["proposal_id"])["proposal"]
                    if value["can_cancel"]:
                        self.tools.cancel(run["owner"],value["id"],{"expected_revision":value["revision"]})
                except (ValueError,RuntimeError,OSError):
                    pass
            if current["state"] not in {"cancelled","interrupted"}:self._deliver(run)
        return True

    def _deliver(self,run):
        self._publish(run)
        if not self.responses or run["source"]!="telegram_owner" or run["origin_update_id"] is None:return
        try:
            current=self.get(run["owner"],run["id"])
        except (ValueError,RuntimeError,OSError):
            return
        result=current["result"] or {}
        body=f"**{current['label']} — result**\n\n"
        if current["state"]!="completed":
            body+=current["error"] or "The run stopped before completion."
        else:
            body+=result.get('summary','Work completed.')
        if result.get("findings"):body+="\n\n"+"\n".join("• "+x for x in result["findings"])
        if current["state"]=="completed":
            body+="\n\n"+("Applied and hash-verified." if result.get("implementation")=="applied_verified"
                          else "Read/review work completed; no file was applied.")
        try:
            self.responses.queue(run["owner"],run["origin_update_id"],{
                "text":body,"report_id":"team-run:"+run["id"],"task":run["project"],
                "kind":"team_result","source":{"run_id":run["id"]},"assessed_at":None,"retrieved_at":self.clock(),
            },expected_preference_revision=run["preference_revision"])
        except (ValueError,RuntimeError,OSError):
            with self.store.db() as db:
                db.execute("UPDATE team_runs SET error=? WHERE id=?",
                           ("The result is saved; Telegram delivery could not be queued.",run["id"]))

    def _publish(self,run):
        if self.publish and run["origin_update_id"] is not None:
            try:
                self.publish(self.get(run["owner"],run["id"]),run)
            except (ValueError,RuntimeError,OSError):
                # Delivery cannot turn a settled implementation receipt into a
                # failed execution or authorize replaying the underlying work.
                return False
        return True

    def cancel(self,owner,identity):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE");self._principal(db,owner,enabled=False)
            row=self._row(db,owner,identity)
            if row["state"]=="applying":raise ConflictError("The reviewed write has started. Its receipt must settle first.")
            if row["state"] in TERMINAL:return self._public(db,row)
            db.execute("UPDATE team_runs SET state='cancelled',updated_at=?,error=? WHERE id=?",
                       (self.clock(),"Stopped by the owner. Any already-admitted model request may still finish.",identity))
            proposal=row["proposal_id"]
            db.execute("UPDATE team_agent_tasks SET state='cancelled',finished_at=? "
                       "WHERE run_id=? AND state IN ('queued','running')",(self.clock(),identity))
            db.execute("UPDATE team_nodes SET state='stopped',finished_at=? "
                       "WHERE run_id=? AND state='running'",(self.clock(),identity))
        if proposal:
            value=self.tools.get(owner,proposal)["proposal"]
            if value["can_cancel"]:
                self.tools.cancel(owner,proposal,{"expected_revision":value["revision"]})
        self._publish(dict(row))
        return self.get(owner,identity)

    @staticmethod
    def _approval_notice(run):
        return (f"Vega prepared a change to {run['path']}.\n"
                f"Review the exact diff under Agents & skills in our conversation or Projects before applying it.\nRun: {run['id']}")

    def delivery_reason(self,db,row):
        identity=row["event_id"].removeprefix("team-review:")
        run=db.execute("SELECT * FROM team_runs WHERE id=?",(identity,)).fetchone()
        if not run:return "The team review notice has no owner task."
        try:
            self._guard(db,run["owner"],identity,{"awaiting_approval"})
            proposal=db.execute("SELECT state FROM project_tool_proposals WHERE id=?",(run["proposal_id"],)).fetchone()
            if not proposal or proposal["state"]!="awaiting_approval" or row["text"]!=self._approval_notice(run):
                return "The proposed change or its review state changed."
        except (ValueError,RuntimeError,OSError):
            return "The team review notice is no longer eligible."
        return None

    def apply(self,owner,identity,payload):
        if payload.get("approved") is not True:raise ValueError("Review the exact diff before applying.")
        with self.store.db() as db:
            self._principal(db,owner)
            prior=self._row(db,owner,identity)
            if prior["state"]=="completed":return self._public(db,prior)
            row=dict(self._guard(db,owner,identity,{"awaiting_approval"}))
            if json.loads(row["binding"]).get("delegation_version")==1:
                review=db.execute("SELECT review,review_binding FROM team_delegations WHERE run_id=?",(identity,)).fetchone()
                proposal=db.execute("SELECT review_run,review_state,review_binding FROM project_tool_proposals WHERE id=?",
                                    (row["proposal_id"],)).fetchone()
                if (not review or not review["review"] or json.loads(review["review"]).get("status")!="accepted"
                        or not proposal or proposal["review_run"]!=identity or proposal["review_state"]!="accepted"
                        or proposal["review_binding"]!=review["review_binding"]):
                    raise ConflictError("Vega must accept the delegated work before owner application.")
        if not row["proposal_id"]:raise ValueError("This run has no prepared change.")
        def guard(db,phase):
            expected={"awaiting_approval"} if phase=="admit" else {"applying"}
            self._guard(db,owner,identity,expected)
            if phase=="admit":db.execute("UPDATE team_runs SET state='applying',updated_at=? WHERE id=?",(self.clock(),identity))
        proposal=self.tools.apply(owner,row["proposal_id"],payload,guard=guard)["proposal"]
        if proposal["state"]!="applied":
            with self.store.db() as db:
                db.execute("UPDATE team_runs SET state='failed',error=?,updated_at=? WHERE id=?",
                           (proposal["receipt"]["detail"],self.clock(),identity))
            return self.get(owner,identity)
        try:
            self._verify_applied(row,proposal)
        except (ValueError,RuntimeError,OSError) as error:
            with self.store.db() as db:
                existing=self._row(db,owner,identity)
                result=json.loads(existing["result"] or "{}")
                result["implementation"]="applied_verification_pending"
                db.execute("UPDATE team_runs SET state='failed',result=?,error=?,updated_at=? WHERE id=?",
                           (json.dumps(result),"The file was applied, but post-apply verification needs attention: "+str(error)[:300],
                            self.clock(),identity))
        return self.get(owner,identity)

    def _verify_applied(self,run,proposal):
        file=self.tools.read(run["owner"],{**json.loads(run["binding"]),"path":run["path"]})["file"]
        if file["sha256"]!=proposal["after_sha256"]:raise ConflictError("The applied file changed before verification.")
        syntax=verify_syntax(run["path"],file["text"],node=NODE if NODE.exists() else None)
        if syntax["state"]=="failed":raise ValueError("The applied source did not pass syntax verification.")
        with self.store.db() as db:
            row=self._row(db,run["owner"],run["id"])
            result=json.loads(row["result"] or "{}")
            result.update(implementation="applied_verified",applied_sha256=file["sha256"],post_apply_syntax=syntax)
            db.execute("UPDATE team_runs SET state='completed',result=?,updated_at=? WHERE id=?",
                       (json.dumps(result),self.clock(),run["id"]))
        self._deliver(run)

    def check(self,owner,identity,payload):
        if not isinstance(payload,dict) or set(payload)!={"expected_sha256"}:
            raise ValueError("Use the applied file hash to run the registered checks.")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._principal(db,owner)
            row=dict(self._row(db,owner,identity))
            result=json.loads(row["result"] or "{}")
            if (row["state"] not in {"completed","failed"} or result.get("implementation") not in
                    {"applied_verified","applied_verification_pending"}
                    or payload["expected_sha256"]!=result.get("applied_sha256")):
                raise ConflictError("Checks require this exact, completed file application.")
            workspace=db.execute("SELECT root FROM project_tool_workspaces WHERE owner=? AND project=?",
                                 (owner,row["project"])).fetchone()
            recipe=project_checks.recipe_for(row["project"],row["path"],workspace["root"]) if workspace else None
            if not recipe:
                raise ValueError("No execution recipe is registered for this applied file.")
            self.tools._workspace(db,owner,json.loads(row["binding"]))
            if dispatch_target(self.tasks(),row["project"])["thread_id"]!=row["thread_id"]:
                raise ConflictError("The registered project changed.")
            prior=result.get("functional_checks")
            if project_checks.is_current(prior,payload["expected_sha256"],recipe_id=recipe["id"],path=row["path"]):
                return self._public(db,row)
            pause_revision=self._pause(db)[1]
            db.execute("UPDATE team_runs SET state='verifying',updated_at=? WHERE id=?",(self.clock(),identity))
        try:
            checks=project_checks.execute(payload["expected_sha256"],recipe_id=recipe["id"],path=row["path"])
        except (OSError,ValueError,RuntimeError):
            checks={"recipe":recipe["id"],"state":"failed","detail":"The registered checks could not run."}
        try:
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                current=self._row(db,owner,identity)
                # Cancellation and owner changes win over a late child-process result.
                self._principal(db,owner)
                if current["state"]!="verifying":
                    return self._public(db,current)
                self.tools._workspace(db,owner,json.loads(row["binding"]))
                if self._pause(db)[1]!=pause_revision:
                    raise ConflictError("A later pause stopped this verification.")
                result=json.loads(current["result"] or "{}")
                passed=checks["state"]=="passed"
                result.update(functional_tests_run="started_at" in checks,functional_checks=checks,
                              implementation="applied_verified" if passed else "applied_verification_pending",
                              summary="Change applied. "+checks["detail"])
                db.execute("UPDATE team_runs SET state=?,result=?,error=?,updated_at=? WHERE id=?",
                           ("completed" if passed else "failed",json.dumps(result),
                            None if passed else checks["detail"],self.clock(),identity))
        except (PermissionError,ConflictError,ValueError):
            with self.store.db() as db:
                db.execute("UPDATE team_runs SET state='interrupted',error=?,updated_at=? "
                           "WHERE id=? AND owner=? AND state='verifying'",
                           ("The verification scope changed; its late result was discarded.",self.clock(),identity,owner))
            raise
        self._deliver(row)
        return self.get(owner,identity)

    def active(self):
        with self.store.db() as db:
            return db.execute("SELECT count(*) FROM team_runs WHERE provider_active>0 OR state IN "
                              "('planning','reading','delegating','preparing','reviewing','verifying','applying')").fetchone()[0]

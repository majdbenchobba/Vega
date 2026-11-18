"""One explicitly consented failed recording, encrypted locally and bounded."""
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from companion import ConflictError
from relay import protect

MAX_PCM=12*16000*2
MAX_BLOB=800000
ARM_TTL=15*60
RETAIN_TTL=30*60
CONSENT_VERSION="one-failed-local-clip-v1"
OWNER_CHECK_CONSENT_VERSION="one-failed-owner-check-v1"
GREETING_PROBE_CONSENT_VERSION="one-four-greetings-probe-v1"
CONSENTS={"enrollment":CONSENT_VERSION,"owner_check":OWNER_CHECK_CONSENT_VERSION,
          "greeting_probe":GREETING_PROBE_CONSENT_VERSION}


class VoiceDiagnostics:
    def __init__(self,store,*,clock=time.time,protect_fn=protect):
        self.store,self.clock,self.protect=store,clock,protect_fn
        self.directory=Path(store.directory).absolute()
        with store.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS voice_diagnostics(
                id TEXT PRIMARY KEY,owner INTEGER NOT NULL,request_key TEXT NOT NULL,
                state TEXT NOT NULL,armed_at REAL NOT NULL,deadline REAL NOT NULL,
                captured_at REAL,expires_at REAL,cipher_hash TEXT,info TEXT,
                UNIQUE(owner,request_key))""")
            columns={row["name"] for row in db.execute("PRAGMA table_info(voice_diagnostics)")}
            if "purpose" not in columns:
                db.execute("ALTER TABLE voice_diagnostics ADD COLUMN purpose TEXT NOT NULL DEFAULT 'enrollment'")

    @staticmethod
    def principal(db,owner):
        values={k:json.loads(v) for k,v in db.execute(
            "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
        if type(owner) is not int or values.get("owner_id")!=owner or values.get("owner_chat_id")!=owner:
            raise PermissionError("The paired owner is required.")

    def path(self,identity):
        if not isinstance(identity,str) or not re.fullmatch(r"[a-f0-9]{32}",identity):
            raise ValueError("Invalid diagnostic identity.")
        path=self.directory/("owner-voice-diagnostic-"+identity+".dpapi")
        if (self.directory.resolve()!=self.directory or path.resolve().parent!=self.directory
                or path.is_symlink() or getattr(path,"is_junction",lambda:False)()):
            raise ValueError("The private diagnostic location changed.")
        return path

    @staticmethod
    def public(row):
        if row is None:return {"state":"off"}
        if row["purpose"] not in CONSENTS:
            raise ValueError("The diagnostic recording purpose is unsupported.")
        return {"id":row["id"],"state":row["state"],"deadline":row["deadline"],
                "captured_at":row["captured_at"],"expires_at":row["expires_at"],
                "can_clear":row["state"] in ("armed","stored"),
                "purpose":row["purpose"],"consent_version":CONSENTS[row["purpose"]]}

    def _erase(self,db,row,state):
        if row["state"] in ("armed","stored"):
            path=self.path(row["id"])
            if path.exists():
                body=path.read_bytes()
                if row["cipher_hash"] and hashlib.sha256(body).hexdigest()!=row["cipher_hash"]:
                    raise ConflictError("The diagnostic file changed; it was not removed.")
                path.unlink()
        db.execute("UPDATE voice_diagnostics SET state=?,cipher_hash=NULL WHERE id=?",(state,row["id"]))

    def expire(self):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM voice_diagnostics WHERE "
                                  "(state='armed' AND deadline<=?) OR (state='stored' AND expires_at<=?)",
                                  (self.clock(),self.clock())).fetchall():
                self._erase(db,row,"expired")

    def status(self,owner):
        self.expire()
        with self.store.db() as db:
            self.principal(db,owner)
            row=db.execute("SELECT * FROM voice_diagnostics WHERE owner=? ORDER BY armed_at DESC,rowid DESC LIMIT 1",(owner,)).fetchone()
            public=self.public(row)
            if row and row["state"]=="armed" and self.path(row["id"]).exists():
                public["state"]="needs_review"
            return {**public,"owner_check_supported":True,
                    "owner_check_consent_version":OWNER_CHECK_CONSENT_VERSION,
                    "greeting_probe_supported":True,
                    "greeting_probe_consent_version":GREETING_PROBE_CONSENT_VERSION}

    def arm(self,owner,payload):
        if not isinstance(payload,dict) or set(payload)-{"consent","consent_version","request_id","purpose"}:
            raise ValueError("Use the supported one-clip diagnostic fields.")
        purpose=payload.get("purpose","enrollment")
        if not isinstance(purpose,str) or purpose not in CONSENTS or payload.get("consent") is not True or payload.get("consent_version")!=CONSENTS[purpose]:
            raise ValueError("Consent is required to keep one failed clip encrypted locally.")
        key=payload.get("request_id")
        if not isinstance(key,str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,120}",key):
            raise ValueError("Use a stable diagnostic request identity.")
        self.expire()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE");self.principal(db,owner)
            prior=db.execute("SELECT * FROM voice_diagnostics WHERE owner=? AND request_key=?",(owner,key)).fetchone()
            if prior:
                if prior["purpose"]!=purpose:
                    raise ConflictError("That consent identity belongs to a different recording purpose.")
                return self.public(prior)
            if db.execute("SELECT 1 FROM voice_diagnostics WHERE state IN ('armed','stored')").fetchone():
                raise ConflictError("Finish or clear the existing local diagnostic first.")
            identity=uuid.uuid4().hex
            db.execute("INSERT INTO voice_diagnostics(id,owner,request_key,state,armed_at,deadline,purpose) "
                       "VALUES(?,?,?,'armed',?,?,?)",
                       (identity,owner,key,self.clock(),self.clock()+ARM_TTL,purpose))
            return self.public(db.execute("SELECT * FROM voice_diagnostics WHERE id=?",(identity,)).fetchone())

    def capture_locked(self,db,owner,pcm,result,*,purpose="enrollment",started_at=None):
        """Only the exact consented manual operation can consume its one clip."""
        self.principal(db,owner)
        row=db.execute("SELECT * FROM voice_diagnostics WHERE owner=? AND state='armed'",(owner,)).fetchone()
        if not row:return {"state":"off"}
        if purpose not in CONSENTS or row["purpose"]!=purpose:
            return {"state":"off"}
        if purpose in ("owner_check","greeting_probe") and (type(started_at) not in (int,float) or not math.isfinite(started_at)
                                      or started_at<row["armed_at"]):
            return {"state":"off"}
        if self.clock()>row["deadline"]:
            self._erase(db,row,"expired");return {"state":"expired"}
        if result["accepted"]:
            db.execute("UPDATE voice_diagnostics SET state='not_needed' WHERE id=?",(row["id"],))
            return {"state":"not_needed"}
        if not isinstance(pcm,bytes) or not 0<len(pcm)<=MAX_PCM or len(pcm)%2:
            db.execute("UPDATE voice_diagnostics SET state='not_saved' WHERE id=?",(row["id"],))
            return {"state":"not_saved"}
        payload={"version":2,"owner":owner,"id":row["id"],"sample_rate":16000,"purpose":purpose,
                 "pcm":base64.b64encode(pcm).decode("ascii")}
        path=self.path(row["id"])
        created=False
        try:
            encrypted=self.protect(json.dumps(payload,separators=(",",":")).encode())
            if not isinstance(encrypted,bytes) or not 0<len(encrypted)<=MAX_BLOB:
                raise ValueError("The local diagnostic could not be encrypted.")
            with path.open("xb") as output:
                created=True
                output.write(encrypted)
        except (ValueError,OSError,RuntimeError):
            if created:self.path(row["id"]).unlink(missing_ok=True)
            elif path.exists():
                # A file may have survived an interrupted database commit.
                # Do not overwrite it or retain another clip. Keep its original
                # armed deadline so normal expiry/explicit clear still removes it.
                return {**self.public(row),"state":"needs_review"}
            db.execute("UPDATE voice_diagnostics SET state='not_saved' WHERE id=?",(row["id"],))
            return {"state":"not_saved"}
        captured=self.clock()
        info={"reason":result["reason"],"diagnostics":result["diagnostics"],"purpose":purpose}
        db.execute("UPDATE voice_diagnostics SET state='stored',captured_at=?,expires_at=?,cipher_hash=?,info=? WHERE id=?",
                   (captured,captured+RETAIN_TTL,hashlib.sha256(encrypted).hexdigest(),json.dumps(info),row["id"]))
        return self.public(db.execute("SELECT * FROM voice_diagnostics WHERE id=?",(row["id"],)).fetchone())

    def read_local(self,owner,identity):
        """Local diagnostic code only. Do not expose this method over HTTP."""
        self.expire()
        with self.store.db() as db:
            self.principal(db,owner)
            row=db.execute("SELECT * FROM voice_diagnostics WHERE owner=? AND id=? AND state='stored'",(owner,identity)).fetchone()
            if not row:raise ValueError("There is no current saved diagnostic.")
            path=self.path(identity)
            with path.open("rb") as stream:encrypted=stream.read(MAX_BLOB+1)
            if len(encrypted)>MAX_BLOB or hashlib.sha256(encrypted).hexdigest()!=row["cipher_hash"]:
                raise ConflictError("The encrypted diagnostic changed.")
            plain=self.protect(encrypted,decrypt=True)
            payload=json.loads(plain)
            if not isinstance(payload,dict):
                raise ValueError("The diagnostic binding is invalid.")
            legacy=payload.get("version")==1 and row["purpose"]=="enrollment"
            expected={"version","owner","id","sample_rate","pcm"} if legacy else {
                "version","owner","id","sample_rate","pcm","purpose"}
            if (set(payload)!=expected or not legacy and (payload.get("version")!=2 or payload.get("purpose")!=row["purpose"])
                    or payload.get("owner")!=owner or payload.get("id")!=identity or payload.get("sample_rate")!=16000):
                raise ValueError("The diagnostic binding is invalid.")
            raw=base64.b64decode(payload["pcm"],validate=True)
            if not 0<len(raw)<=MAX_PCM or len(raw)%2:raise ValueError("The diagnostic audio is invalid.")
            return bytearray(raw),json.loads(row["info"])

    def clear(self,owner,identity):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE");self.principal(db,owner)
            row=db.execute("SELECT * FROM voice_diagnostics WHERE owner=? AND id=?",(owner,identity)).fetchone()
            if not row:raise ValueError("This diagnostic is unavailable.")
            if row["state"] in ("armed","stored"):self._erase(db,row,"deleted")
            return self.public(db.execute("SELECT * FROM voice_diagnostics WHERE id=?",(identity,)).fetchone())

    def clear_owner_locked(self,db,owner):
        self.principal(db,owner)
        for row in db.execute("SELECT * FROM voice_diagnostics WHERE owner=? AND state IN ('armed','stored')",
                              (owner,)).fetchall():
            self._erase(db,row,"deleted")

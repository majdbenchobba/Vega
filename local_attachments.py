"""Owner-selected local chat attachments; staging is separate from sending."""
from __future__ import annotations

import base64
from contextlib import ExitStack, contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid
import warnings

from companion import ConflictError
from memory_policy import contains_secret
from project_files import MAX_BYTES, TEXT_EXTENSIONS, text_from_bytes
from project_requests import ProjectRequests

MAX_FILE=10*1024*1024
MAX_MESSAGE=25*1024*1024
MAX_FILES=5
MAX_STORAGE=100*1024*1024
MAX_IMAGE_PIXELS=20_000_000
ID=re.compile(r"[a-f0-9-]{36}")
KEY=re.compile(r"[A-Za-z0-9_-]{16,100}")
IMAGE_MIMES={"PNG":"image/png","JPEG":"image/jpeg","WEBP":"image/webp","GIF":"image/gif"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def filename(value):
    if not isinstance(value,str) or not value.strip() or len(value)>250:
        raise ValueError("Choose a file with a valid name.")
    value=value.replace("\\","/").rsplit("/",1)[-1].strip()
    if not value or value in {".",".."} or re.search(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]",value):
        raise ValueError("The file name contains unsupported characters.")
    return value


def media_type(data,name,claimed):
    if data.startswith((b"\x89PNG\r\n\x1a\n",b"\xff\xd8\xff",b"GIF87a",b"GIF89a")) or (
        data[:4]==b"RIFF" and data[8:12]==b"WEBP"
    ):
        from PIL import Image
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error",Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    if image.format not in IMAGE_MIMES or image.width*image.height>MAX_IMAGE_PIXELS:
                        raise ValueError("This image exceeds the supported image size.")
                    mime=IMAGE_MIMES[image.format]
                    image.verify()
            return "image",mime
        except (OSError,ValueError,Image.DecompressionBombError,Image.DecompressionBombWarning):
            raise ValueError("This image could not be read. Choose a valid PNG, JPEG, WebP or GIF.") from None
    if claimed in IMAGE_MIMES.values():
        raise ValueError("The selected image is incomplete or has an unsupported format.")
    if data.startswith(b"%PDF-"):return "document","application/pdf"
    if Path(name).suffix.lower() in TEXT_EXTENSIONS:
        return "text","text/plain"
    return "file","application/octet-stream"


class LocalAttachments:
    def __init__(self,store,*,clock=time.time):
        self.store,self.clock=store,clock
        self.directory=store.directory/"attachments"
        self.directory.mkdir(exist_ok=True)
        if self.directory.is_symlink() or getattr(self.directory.lstat(),"st_file_attributes",0)&0x400:
            raise ValueError("The private attachment folder cannot be a link.")
        with store.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS local_attachments (
                id TEXT PRIMARY KEY,owner INTEGER NOT NULL,request_key TEXT NOT NULL,
                payload_hash TEXT NOT NULL,name TEXT NOT NULL,mime_type TEXT NOT NULL,
                kind TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,
                state TEXT NOT NULL,created_at REAL NOT NULL,update_id INTEGER,
                UNIQUE(owner,request_key))""")

    def _row(self,db,owner,identity,*,include_removed=False):
        ProjectRequests._principal(db,owner)
        if not isinstance(identity,str) or not ID.fullmatch(identity):
            raise ValueError("Choose an existing attachment.")
        row=db.execute("SELECT * FROM local_attachments WHERE owner=? AND id=?",(owner,identity)).fetchone()
        if not row or row["state"]=="removed" and not include_removed:raise ValueError("This attachment is unavailable.")
        return row

    @staticmethod
    def _public(row):
        return {key:row[key] for key in ("id","name","mime_type","kind","size","sha256","state")} | {
            "preview_url":f"/api/attachments/{row['id']}/content" if row["kind"]=="image" and row["state"]!="removed" else None,
            "content_url":f"/api/attachments/{row['id']}/content" if row["state"]!="removed" else None,
        }

    def stage(self,owner,payload):
        if not isinstance(payload,dict) or set(payload)-{"request_id","name","mime_type","data_base64"}:
            raise ValueError("Use the supported attachment fields.")
        key=payload.get("request_id")
        if not isinstance(key,str) or not KEY.fullmatch(key):raise ValueError("A stable upload identity is required.")
        name=filename(payload.get("name"))
        encoded=payload.get("data_base64")
        if not isinstance(encoded,str) or not 1<=len(encoded)<=4*((MAX_FILE+2)//3):
            raise ValueError("Each attachment must be at most 10 MB.")
        try:data=base64.b64decode(encoded,validate=True)
        except (ValueError,UnicodeError):raise ValueError("The attachment upload is incomplete.") from None
        if not 0<len(data)<=MAX_FILE:raise ValueError("Choose a nonempty file of at most 10 MB.")
        kind,mime=media_type(data,name,payload.get("mime_type"))
        sha=digest(data)
        fingerprint=digest(json.dumps([name,mime,sha]).encode())
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            ProjectRequests._principal(db,owner)
            prior=db.execute("SELECT * FROM local_attachments WHERE owner=? AND request_key=?",(owner,key)).fetchone()
            if prior:
                if prior["payload_hash"]!=fingerprint:raise ConflictError("This upload identity belongs to a different file.")
                return {"attachment":self._public(prior)}
            if db.execute("SELECT COALESCE(sum(size),0) FROM local_attachments WHERE owner=? AND state<>'removed'",(owner,)).fetchone()[0]+len(data)>MAX_STORAGE:
                raise ValueError("Vega's private attachment storage reached its 100 MB limit.")
            if db.execute("SELECT count(*) FROM local_attachments WHERE owner=? AND state='staged'",(owner,)).fetchone()[0]>=30:
                raise ValueError("Send or remove some staged attachments before adding more.")
            identity=str(uuid.uuid4())
            path=self.directory/(identity+".blob")
            created=False
            try:
                with path.open("xb") as stream:
                    created=True
                    stream.write(data)
            except OSError:
                if created and path.resolve().parent==self.directory.resolve():
                    path.unlink(missing_ok=True)
                raise
            db.execute("INSERT INTO local_attachments VALUES(?,?,?,?,?,?,?,?,?,'staged',?,NULL)",
                       (identity,owner,key,fingerprint,name,mime,kind,len(data),sha,self.clock()))
            return {"attachment":self._public(self._row(db,owner,identity))}

    def bind(self,db,owner,identities,update_id):
        ProjectRequests._principal(db,owner)
        if (not isinstance(identities,list) or len(identities)>MAX_FILES
                or any(not isinstance(value,str) for value in identities)
                or len(set(identities))!=len(identities)):
            raise ValueError("Attach up to five different files.")
        rows=[self._row(db,owner,value) for value in identities]
        if sum(row["size"] for row in rows)>MAX_MESSAGE:raise ValueError("Attachments in one message must total at most 25 MB.")
        for row in rows:
            if row["state"]=="attached" and row["update_id"]!=update_id:
                raise ConflictError("This file already belongs to another message.")
            if row["state"]=="staged" and self.clock()-row["created_at"]>86400:
                raise ValueError("A staged file expired. Attach it again.")
        for row in rows:
            db.execute("UPDATE local_attachments SET state='attached',update_id=? WHERE id=?",(update_id,row["id"]))

    def remove(self,owner,identity):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row=self._row(db,owner,identity,include_removed=True)
            if row["state"]=="removed":return {"attachment":self._public(row)}
            if row["state"]!="staged":raise ConflictError("A sent attachment is part of its conversation record.")
            path=self.directory/(identity+".blob")
            with self._file(row) as stream:
                if digest(stream.read(MAX_FILE+1))!=row["sha256"]:
                    raise ConflictError("The stored attachment changed.")
            if path.resolve().parent!=self.directory.resolve():
                raise ConflictError("The private attachment location changed.")
            path.unlink()
            db.execute("UPDATE local_attachments SET state='removed' WHERE id=?",(identity,))
            result=dict(row);result["state"]="removed"
            return {"attachment":self._public(result)}

    def for_message(self,owner,update_id):
        with self.store.db() as db:
            return self.for_messages(db,owner,[update_id]).get(update_id,[])

    def for_messages(self,db,owner,update_ids):
        """Read a bounded group inside the caller's current database snapshot."""
        ProjectRequests._principal(db,owner)
        if len(update_ids)>80 or any(type(value) is not int for value in update_ids):
            raise ValueError("Choose at most 80 message identities.")
        identities=list(dict.fromkeys(update_ids))
        if not identities:return {}
        placeholders=",".join("?" for _ in identities)
        rows=db.execute(
            f"SELECT * FROM local_attachments WHERE owner=? AND update_id IN ({placeholders}) "
            "AND state='attached' ORDER BY rowid",(owner,*identities)).fetchall()
        grouped={}
        for row in rows:
            grouped.setdefault(row["update_id"],[]).append(self._public(row))
        return grouped

    @contextmanager
    def _file(self,row):
        path=self.directory/(row["id"]+".blob")
        with ExitStack() as stack:
            if os.name=="nt":
                from project_files import _windows_handle
                parent=Path(path.anchor)
                stack.enter_context(_windows_handle(parent,directory=True))
                for piece in path.parent.parts[1:]:
                    parent=parent/piece
                    stack.enter_context(_windows_handle(parent,directory=True))
                stream=stack.enter_context(_windows_handle(path,directory=False))
            else:
                for parent in [*reversed(path.parent.parents),path.parent]:
                    if parent.is_symlink():raise ValueError("Attachment links are unavailable.")
                descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
                stream=stack.enter_context(os.fdopen(descriptor,"rb"))
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size!=row["size"]:
                raise ConflictError("The stored attachment changed.")
            yield stream

    def content(self,owner,identity):
        with self.store.db() as db:row=dict(self._row(db,owner,identity))
        with self._file(row) as stream:data=stream.read(MAX_FILE+1)
        if len(data)!=row["size"] or digest(data)!=row["sha256"]:raise ConflictError("The stored attachment changed.")
        with self.store.db() as db:
            if dict(self._row(db,owner,identity))!=row:raise ConflictError("The attachment changed during the read.")
        return data,self._public(row)

    @staticmethod
    def _image(data):
        from PIL import Image,ImageOps
        with Image.open(io.BytesIO(data)) as opened:
            image=ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail((1800,1800))
            out=io.BytesIO();image.save(out,format="PNG")
            mime="image/png"
            if out.tell()>1024*1024:
                out=io.BytesIO();image.save(out,format="JPEG",quality=85)
                mime="image/jpeg"
            if out.tell()>2*1024*1024:
                image.thumbnail((1200,1200))
                out=io.BytesIO();image.save(out,format="JPEG",quality=75)
        return "data:"+mime+";base64,"+base64.b64encode(out.getvalue()).decode("ascii")

    def model_context(self,owner,update_id):
        sources,images=[],[]
        attachments=self.for_message(owner,update_id)
        text_budget=max(1,12000//max(1,sum(item["kind"]!="image" for item in attachments)))
        for attachment in attachments:
            data,metadata=self.content(owner,attachment["id"])
            source={**metadata,"authority":False,"content_status":"metadata_only"}
            if metadata["kind"]=="image":
                images.append({"attachment_id":metadata["id"],"image_url":self._image(data)})
                source["content_status"]="image_supplied"
                source["image_note"]="A normalized still image is supplied; animation uses its first frame."
            elif metadata["kind"]=="text" and len(data)<=MAX_BYTES:
                try:
                    value=text_from_bytes(data)
                    source.update(text=value[:text_budget],truncated=len(value)>text_budget,content_status="text_supplied")
                except ValueError:source["content_status"]="text_unavailable"
            elif metadata["mime_type"]=="application/pdf":
                try:
                    from pypdf import PdfReader
                    reader=PdfReader(io.BytesIO(data))
                    if reader.is_encrypted:raise ValueError("Encrypted PDF")
                    pieces=[];remaining=text_budget
                    for page in reader.pages[:10]:
                        value=page.extract_text() or ""
                        pieces.append(value[:remaining]);remaining-=min(remaining,len(value))
                        if remaining<=0:break
                    value="\n".join(pieces)
                    if contains_secret(value):raise ValueError("Credential-like PDF text")
                    source.update(text=value,content_status="text_supplied" if value.strip() else "metadata_only",
                                  truncated=remaining<=0 or len(reader.pages)>10,pages=len(reader.pages))
                except Exception:
                    # Malformed owner-supplied documents remain downloadable
                    # metadata; parser exceptions cannot strand a chat turn.
                    source["content_status"]="text_unavailable"
            sources.append(source)
        if sum(len(item["image_url"]) for item in images)>14*1024*1024:
            raise ValueError("These images are too large for one model request. Send fewer images together.")
        # Revalidate the principal after extraction; file contents never select actions.
        with self.store.db() as db:ProjectRequests._principal(db,owner)
        return {"sources":sources,"images":images}

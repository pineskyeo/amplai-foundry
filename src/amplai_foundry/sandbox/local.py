"""Data-only sandbox. No shell, interpreter, import, network, or ambient path access.

This is containment for the built-in declarative driver ONLY. It is never advertised
as an OS sandbox suitable for executing arbitrary model-generated source code.
"""
from __future__ import annotations
import json
import os
import tempfile
import stat
from pathlib import Path,PurePosixPath
from amplai_foundry.runtime.errors import Hold,RuntimeFault
from amplai_foundry.runtime.contracts.identity import canonical,digest_bytes

class DataSandbox:
    def __init__(self,root: Path, *,max_bytes: int=8*1024*1024,protected: tuple[str,...]=('.git','.ai-team','tests','verifiers')):
        self.root=Path(root)
        if self.root.is_symlink(): raise RuntimeFault('SYMLINK_ESCAPE','Sandbox root cannot be a symlink')
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700); self.root=self.root.resolve()
        self.max_bytes,self.protected=max_bytes,protected
    def _parts(self,name: str) -> tuple[str,...]:
        path=PurePosixPath(name)
        if not name or path.is_absolute() or '..' in path.parts or '\\' in name or '\x00' in name or not path.parts:
            raise RuntimeFault('SANDBOX_PATH','Path must be a relative sandbox path without traversal')
        if any(part.startswith('.') or part in self.protected for part in path.parts):
            raise RuntimeFault('PROTECTED_PATH','Agent output cannot modify hidden governance or verifier paths')
        return path.parts
    def _parent(self,name: str,create: bool=False):
        parts=self._parts(name); fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                if create:
                    try: os.mkdir(part,mode=0o700,dir_fd=fd)
                    except FileExistsError: pass
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                os.close(fd);fd=child
            return fd,parts[-1]
        except (OSError,RuntimeFault) as exc:
            os.close(fd)
            if isinstance(exc,RuntimeFault): raise
            raise RuntimeFault('SANDBOX_PATH','Parent path is unavailable or a symlink') from exc
    def write(self,name: str,data: bytes, *,expected_old_digest: str | None=None):
        if len(data)>self.max_bytes: raise Hold('SANDBOX_QUOTA','Single output exceeds the sandbox quota')
        existing_size=sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file() and not p.is_symlink())
        if existing_size+len(data)>self.max_bytes*2: raise Hold('SANDBOX_QUOTA','Sandbox aggregate output quota exceeded')
        fd,leaf=self._parent(name,True);tmp='.tmp-'+__import__('uuid').uuid4().hex
        try:
            try:
                oldfd=os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
            except FileNotFoundError:
                if expected_old_digest is not None: raise Hold('SOURCE_CHANGED','Expected file no longer exists')
            else:
                with os.fdopen(oldfd,'rb') as src:
                    info=os.fstat(src.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise Hold('SANDBOX_SPECIAL','Existing path is not an unlinked ordinary file')
                    old=src.read(self.max_bytes+1)
                if expected_old_digest is not None and digest_bytes(old)!=expected_old_digest: raise Hold('SOURCE_CHANGED','File changed since planning')
            outfd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            with os.fdopen(outfd,'wb') as out: out.write(data);out.flush();os.fsync(out.fileno())
            os.rename(tmp,leaf,src_dir_fd=fd,dst_dir_fd=fd);os.fsync(fd)
        except OSError as exc: raise RuntimeFault('SANDBOX_WRITE','Output path is not a writable regular sandbox file') from exc
        finally:
            try: os.unlink(tmp,dir_fd=fd)
            except FileNotFoundError: pass
            os.close(fd)
    def read(self,name: str) -> bytes:
        fd,leaf=self._parent(name)
        try:
            f=os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
            with os.fdopen(f,'rb') as src:
                info=os.fstat(src.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise Hold('SANDBOX_SPECIAL','Only ordinary non-hardlinked files may be read')
                data=src.read(self.max_bytes+1)
            if len(data)>self.max_bytes: raise Hold('SANDBOX_QUOTA','Input exceeds sandbox quota')
            return data
        except OSError as exc: raise RuntimeFault('SANDBOX_READ','Path must be a regular in-sandbox file') from exc
        finally: os.close(fd)
    def execute(self,operations: list[dict]) -> None:
        if not isinstance(operations,list) or len(operations)>256: raise Hold('OPERATION_LIMIT','Declarative operation count exceeds the configured limit')
        # Prevalidate the complete plan before executing any write.
        for op in operations:
            if op.get('op') not in {'write_text','write_json','merge_json'}: raise RuntimeFault('OP_NOT_ALLOWED','No arbitrary code/command operation exists in this sandbox')
            if set(op)-{'op','path','content','expected_old_digest'}: raise RuntimeFault('OP_FIELDS','Unknown operation fields')
            self._parts(op['path'])
            if op['op']=='write_text' and not isinstance(op.get('content'),str): raise RuntimeFault('OP_CONTENT','write_text needs a string')
            if len(canonical(op))>self.max_bytes: raise Hold('SANDBOX_QUOTA','Operation payload exceeds quota')
        for op in operations:
            content=op['content']
            if op['op']=='write_text': data=content.encode('utf-8')
            elif op['op']=='write_json': data=canonical(content)
            else:
                old=json.loads(self.read(op['path']))
                if not isinstance(old,dict) or not isinstance(content,dict): raise RuntimeFault('MERGE_TYPE','merge_json requires JSON objects')
                data=canonical({**old,**content})
            self.write(op['path'],data,expected_old_digest=op.get('expected_old_digest'))

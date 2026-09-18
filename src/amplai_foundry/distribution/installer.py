"""Ownership-aware Kit installation with signed plans, backups and crash rollback."""
from __future__ import annotations
import contextlib,fcntl,json,os,shutil,tempfile
from pathlib import Path
from .packs import safe_path
from ..runtime.contracts.identity import canonical,digest,digest_bytes,new_id,now
from ..runtime.errors import Conflict,Hold,RuntimeFault

class KitInstaller:
    ALLOWED_ROOTS={'.ai-team','.agents','.claude','.codex','.opencode','AGENTS.md','CLAUDE.md'}
    def __init__(self,registry):self.registry=registry
    def _path(self,root,name):
        relative=safe_path(name)
        if relative.parts[0] not in self.ALLOWED_ROOTS:raise Hold('INSTALL_SCOPE','Kit may not overwrite application source or arbitrary home paths')
        current=root
        for part in relative.parts:
            current=current/part
            if current.is_symlink():raise Hold('INSTALL_SYMLINK','Kit installation refuses all symlink path components')
        if current.exists() and not current.is_file():raise Hold('INSTALL_TYPE','Owned payload target must be a regular file')
        return current
    @staticmethod
    def _atomic(path,data):
        path.parent.mkdir(parents=True,exist_ok=True)
        fd,temp=tempfile.mkstemp(prefix='.v3-',dir=path.parent)
        try:
            with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
            os.replace(temp,path)
            directory=os.open(path.parent,os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(temp):os.unlink(temp)
    def _metadata(self,root):
        root=Path(root).absolute()
        if root.is_symlink() or not root.is_dir():raise Hold('INSTALL_ROOT','An existing non-symlink repository root is required')
        meta=root/'.ai-team'/'install-v3'
        for parent in [root/'.ai-team',meta]:
            if parent.is_symlink():raise Hold('INSTALL_SYMLINK','Kit metadata cannot use symlinks')
        return root,meta
    def plan(self,root,bundle):
        root,meta=self._metadata(root);manifest,files=self.registry.inspect(bundle)
        receipt_path=meta/(manifest['pack_id']+'.json')
        if '/' in manifest['pack_id'] or '..' in manifest['pack_id']:raise Hold('PACK_ID_PATH','Pack ID cannot be used as an unsafe filename')
        receipt=json.loads(receipt_path.read_bytes()) if receipt_path.exists() else None
        previous=receipt['owned_files'] if receipt else {};changes=[]
        for name in sorted(set(files)|set(previous)):
            path=self._path(root,name);before=digest_bytes(path.read_bytes()) if path.exists() else None
            owned=previous.get(name)
            if owned and before!=owned:raise Hold('LOCAL_MODIFICATION','User modified an owned file; reconcile before install',details={'path':name})
            if not owned and before is not None:
                # A coincidentally identical unowned file is still not silently claimed.
                raise Hold('UNOWNED_FILE','Existing user content is not Kit-owned',details={'path':name})
            after=digest_bytes(files[name]) if name in files else None
            changes.append({'path':name,'before':before,'after':after,'action':'remove' if after is None else 'add' if before is None else 'keep' if before==after else 'replace'})
        result={'schema_version':'3.0.0','plan_id':new_id('install'),'root':str(root),'pack_id':manifest['pack_id'],'version':manifest['version'],
                'bundle_digest':digest(bundle),'previous_receipt_digest':digest_bytes(receipt_path.read_bytes()) if receipt else None,
                'changes':changes,'created_at':now(),'requested_permissions_are_grants':False}
        result['plan_digest']=digest(result);return result
    def apply(self,actor,root,bundle,plan, *,inject_failure=None):
        actor.require('pack.install');root,meta=self._metadata(root)
        if plan.get('root')!=str(root) or plan.get('bundle_digest')!=digest(bundle) or plan.get('plan_digest')!=digest({k:v for k,v in plan.items() if k!='plan_digest'}):raise Hold('INSTALL_PLAN','The explicit install plan does not match root or signed bundle')
        manifest,files=self.registry.inspect(bundle);meta.mkdir(parents=True,exist_ok=True)
        lock=open(meta/'owner.lock','a+b')
        try:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc:raise Hold('INSTALL_BUSY','Another installer owns this repository') from exc
            pending=meta/'pending.json'
            if pending.exists():raise Hold('INSTALL_RECOVERY','Reconcile a previous incomplete install first')
            receipt_path=meta/(manifest['pack_id']+'.json')
            previous=receipt_path.read_bytes() if receipt_path.exists() else None
            if (digest_bytes(previous) if previous else None)!=plan['previous_receipt_digest']:raise Conflict('INSTALL_RECEIPT_CAS','Kit receipt changed since planning')
            for change in plan['changes']:
                path=self._path(root,change['path']);actual=digest_bytes(path.read_bytes()) if path.exists() else None
                if actual!=change['before']:raise Conflict('INSTALL_PREIMAGE','File changed after dry-run')
            backups=meta/'backups'/plan['plan_id'];backups.mkdir(parents=True,mode=0o700)
            for i,change in enumerate(plan['changes']):
                if change['before'] is not None:shutil.copy2(self._path(root,change['path']),backups/str(i))
            if previous:self._atomic(backups/'receipt.json',previous)
            journal={'plan':plan,'backup_dir':str(backups.relative_to(root)),'previous_receipt_exists':previous is not None,'status':'prepared'}
            self._atomic(pending,canonical(journal))
            if inject_failure:inject_failure('prepared')
            for i,change in enumerate(plan['changes']):
                path=self._path(root,change['path'])
                if change['action']=='remove':path.unlink()
                elif change['action']!='keep':self._atomic(path,files[change['path']])
                if inject_failure:inject_failure('file:'+str(i))
            owned={name:digest_bytes(content) for name,content in files.items()}
            for name,expected in owned.items():
                if digest_bytes(self._path(root,name).read_bytes())!=expected:raise Hold('INSTALL_POSTIMAGE','Installed bytes differ from signed payload')
            receipt={'schema_version':'3.0.0','receipt_id':new_id('receipt'),'pack_id':manifest['pack_id'],'version':manifest['version'],
                     'bundle_digest':digest(bundle),'plan_digest':plan['plan_digest'],'owned_files':owned,'installed_by':actor.subject_id,'installed_at':now()}
            self._atomic(receipt_path,canonical(receipt));journal['status']='committed';self._atomic(pending,canonical(journal))
            if inject_failure:inject_failure('committed')
            pending.unlink();return receipt
        except Exception:
            # Leave durable journal for explicit restore; never claim successful install.
            raise
        finally:fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
    def recover(self,actor,root):
        actor.require('pack.install');root,meta=self._metadata(root);pending=meta/'pending.json'
        if not pending.exists():return {'status':'clean'}
        with open(meta/'owner.lock','a+b') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc:raise Hold('INSTALL_BUSY','Installer is active') from exc
            journal=json.loads(pending.read_bytes());plan=journal['plan'];backups=root/journal['backup_dir']
            if root not in backups.resolve().parents or backups.is_symlink():raise Hold('INSTALL_BACKUP','Unsafe recovery backup path')
            if journal['status']=='committed':pending.unlink();return {'status':'committed_receipt_preserved'}
            for i,change in reversed(list(enumerate(plan['changes']))):
                path=self._path(root,change['path']);actual=digest_bytes(path.read_bytes()) if path.exists() else None
                if actual not in {change['before'],change['after']}:raise Hold('RECOVERY_CONCURRENT_EDIT','External edit found; automatic rollback will not erase it')
                if change['before'] is None:
                    if path.exists():path.unlink()
                else:
                    source=backups/str(i)
                    if file_digest(source)!=change['before']:raise Hold('BACKUP_INTEGRITY','Install preimage backup differs')
                    self._atomic(path,source.read_bytes())
            receipt_path=meta/(plan['pack_id']+'.json')
            if journal['previous_receipt_exists']:self._atomic(receipt_path,(backups/'receipt.json').read_bytes())
            elif receipt_path.exists():receipt_path.unlink()
            pending.unlink();return {'status':'rolled_back_to_preimages','plan_id':plan['plan_id']}

def file_digest(path):return digest_bytes(path.read_bytes())

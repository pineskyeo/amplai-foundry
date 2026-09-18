"""Online SQLite + immutable CAS backup, explicit restore and reconciliation.

Backups are not grants. Restore enters a kill-switched state and raises the owner
epoch; persisted authority must still be checked against live governance.
"""
from __future__ import annotations
import hashlib,json,os,shutil,sqlite3
from pathlib import Path
from ..contracts.identity import canonical,digest_bytes,new_id,now
from ..storage.store import Store,Scope
from ..errors import Conflict,Hold,RuntimeFault

def file_digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1048576),b''): h.update(b)
    return 'sha256:'+h.hexdigest()

class RecoveryService:
    def __init__(self,store): self.store=store
    def backup(self,actor,destination):
        actor.require('runtime.backup');self.store.assert_outside_tx()
        destination=Path(destination).absolute()
        if destination.exists(): raise Conflict('BACKUP_EXISTS','Backup destination must be new')
        if self.store.root in destination.parents: raise Hold('BACKUP_LOCATION','Backup must not be inside the live store')
        staging=destination.with_name(destination.name+'.staging-'+new_id('backup'))
        staging.mkdir(parents=True,mode=0o700)
        try:
            database=staging/'runtime.sqlite3';self.store.backup(database)
            # Snapshot all registered artifacts from the consistent DB, not a moving live registry.
            db=sqlite3.connect(database);db.row_factory=sqlite3.Row
            rows=db.execute('SELECT * FROM artifacts').fetchall();objects=[]
            for row in rows:
                scope=Scope(row['tenant'],row['project'])
                from ..evidence.cas import ArtifactStore
                cas=ArtifactStore(self.store)
                artifact={'id':row['id'],'digest':row['digest'],'media_type':row['media_type'],'size_bytes':row['size_bytes']}
                content=cas.read(scope,artifact)
                src=cas._path(scope,row['digest'])
                relative=src.relative_to(self.store.root)
                target=staging/relative;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                if not target.exists():
                    with open(target,'xb') as f: f.write(content);f.flush();os.fsync(f.fileno())
                objects.append({'path':relative.as_posix(),'digest':row['digest'],'size_bytes':len(content)})
            scopes=[{'tenant_id':x[0],'project_id':x[1]} for x in db.execute('SELECT DISTINCT tenant,project FROM heads')]
            db.close()
            manifest={'schema_version':'3.0.0','backup_id':new_id('backup'),'created_at':now(),
                      'database_digest':file_digest(database),'artifacts':objects,'scopes':scopes,'source_owner_epoch':self.store.epoch,
                      'restore_policy':'raise epoch; kill admission; reconcile processes/effects; check live authority'}
            (staging/'backup-manifest.json').write_bytes(canonical(manifest))
            os.replace(staging,destination)
            return manifest
        except BaseException:
            shutil.rmtree(staging,ignore_errors=True);raise
    @staticmethod
    def restore(backup,destination, *,operator_confirmed=False):
        if not operator_confirmed: raise Hold('RESTORE_CONFIRMATION','Explicit operator restore confirmation required')
        backup=Path(backup).absolute();destination=Path(destination).absolute()
        if destination.exists() and any(destination.iterdir()): raise Hold('RESTORE_NOT_EMPTY','Restore never overwrites a live runtime')
        manifest=json.loads((backup/'backup-manifest.json').read_bytes())
        if manifest.get('schema_version')!='3.0.0': raise Hold('BACKUP_SCHEMA','Unsupported backup major version')
        database=backup/'runtime.sqlite3'
        if database.is_symlink() or file_digest(database)!=manifest['database_digest']: raise Hold('BACKUP_INTEGRITY','Database digest differs')
        for item in manifest['artifacts']:
            relative=Path(item['path'])
            if relative.is_absolute() or '..' in relative.parts or relative.parts[0]!='artifacts': raise Hold('BACKUP_PATH','Invalid backup artifact path')
            source=backup/relative
            if source.is_symlink() or backup not in source.resolve().parents or file_digest(source)!=item['digest'] or source.stat().st_size!=item['size_bytes']:
                raise Hold('BACKUP_INTEGRITY','Artifact digest/path differs')
        destination.mkdir(parents=True,exist_ok=True,mode=0o700)
        shutil.copy2(database,destination/'runtime.sqlite3')
        for item in manifest['artifacts']:
            target=destination/item['path'];target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            if not target.exists(): shutil.copy2(backup/item['path'],target)
        store=Store(destination)
        try:
            with store.tx() as db:
                for scope_wire in manifest['scopes']:
                    scope=Scope.parse(scope_wire)
                    try: head=store.head(scope,'runtime-control','kill',db=db)
                    except RuntimeFault as exc:
                        if exc.code!='NOT_FOUND': raise
                        head={'row_version':0}
                    store.cas(db,scope,'runtime-control','kill',head['row_version'],'enabled',{'enabled':True,'reason':'Restored: reconcile before new admission','actor':'restore-operator'})
                    store.event(db,scope,'release',manifest['backup_id'],'runtime.restored',{'backup_id':manifest['backup_id'],'owner_epoch':store.epoch,'authority_restored':False})
            return {'status':'restored_admission_disabled','owner_epoch':store.epoch,'backup_id':manifest['backup_id']}
        finally: store.close()

class OutboxPump:
    """At-least-once delivery with stable event IDs; acknowledgments are not executions."""
    def __init__(self,store): self.store=store
    def pump(self,deliver, *,limit=100):
        self.store.assert_outside_tx()
        with self.store._lock:
            rows=self.store.conn.execute('SELECT e.*,o.attempts FROM outbox o JOIN events e ON e.seq=o.event_seq WHERE o.status=? AND o.next_at<=? ORDER BY e.seq LIMIT ?',('pending',self.store.clock(),min(limit,1000))).fetchall()
        results=[]
        for row in rows:
            event={**dict(row),'data':json.loads(row['data'])}
            try: acknowledged=deliver(event) is True
            except Exception: acknowledged=False
            with self.store.tx() as db:
                if acknowledged: db.execute("UPDATE outbox SET status='acknowledged',ack_at=?,attempts=attempts+1 WHERE event_seq=?",(now(),row['seq']))
                else: db.execute("UPDATE outbox SET attempts=attempts+1,next_at=? WHERE event_seq=?",(self.store.clock()+min(300,2**min(row['attempts'],8)),row['seq']))
            results.append({'event_id':row['event_id'],'acknowledged':acknowledged})
        return results

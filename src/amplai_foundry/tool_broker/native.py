"""Native tool IDs -> one durable AMPLAI effect, with no implicit retry.

Bindings are server-installed names, not prompt-supplied imports or URLs. A lost
response retains UNKNOWN even if a daemon adapter later returns. Only trusted
read-only reconciliation may establish the external outcome.
"""
from __future__ import annotations
import queue
import threading
from ..runtime.contracts.identity import canonical, digest, new_id, now
from ..runtime.contracts.registry import strict_json_loads
from ..runtime.errors import Hold, Conflict, RuntimeFault


def invoke_bounded(tool, args, effect_key):
    results = queue.Queue(maxsize=1)
    def run():
        try:results.put((True, tool.invoke(args,effect_key,tool.timeout_seconds)))
        except Exception as exc:results.put((False,exc))
    threading.Thread(target=run,name='amplai-tool-adapter',daemon=True).start()
    try:success,value=results.get(timeout=tool.timeout_seconds)
    except queue.Empty as exc:
        raise Hold('TOOL_TIMEOUT','Adapter deadline elapsed; external outcome remains UNKNOWN') from exc
    if not success:raise value
    return value


class NativeToolBroker:
    def __init__(self,effects,bindings:dict[str,dict]):
        self.effects,self.runtime,self.store=effects,effects.runtime,effects.store
        self.bindings=dict(bindings)
        for name,ref in self.bindings.items():
            if not isinstance(name,str) or not name or name.startswith('-'):raise RuntimeFault('NATIVE_TOOL_NAME','Invalid server-installed tool name')
            effects.tools.get(ref)
    def invoke(self,worker,envelope,*,session_handle:str,native_turn_id:str,native_call_id:str,name:str,arguments:str):
        worker.require('worker.execute');scope=worker.scope
        self.runtime.contracts.validate('execution-envelope',envelope)
        if envelope['scope']!=scope.wire():raise Hold('TOOL_SCOPE','Cross-project call rejected')
        for value in (session_handle,native_turn_id,native_call_id):
            if not isinstance(value,str) or not value or len(value)>512 or any(ord(c)<32 for c in value):
                raise Hold('CALL_CORRELATION','Exact native session/turn/call IDs are required')
        if len(arguments.encode())>1024*1024:raise Hold('TOOL_ARGS_LIMIT','Arguments exceed bound')
        if name not in self.bindings:raise Hold('TOOL_NOT_INSTALLED','Unknown native tool name')
        args=strict_json_loads(arguments);tool=self.effects.tools.get(self.bindings[name])
        cap={'action':tool.action,'resource':tool.resource,'effect_class':tool.effect_class}
        if cap not in envelope['effective_capabilities']:raise Hold('TOOL_CAPABILITY','Tool is not in the execution envelope')
        run=self.store.head(scope,'run',envelope['run_id'])
        if run['data'].get('session_handle')!=session_handle:raise Hold('TOOL_SESSION','Call session differs from current exact run')
        identity={'run_id':envelope['run_id'],'session':session_handle,'turn':native_turn_id,'call':native_call_id}
        call_id='native-'+digest(identity)[7:47]
        request_digest=digest({'identity':identity,'name':name,'args':args,'contract_ref':envelope['contract_ref'],'graph_ref':envelope['graph_ref']})
        artifact=self.runtime.artifacts.admit(scope,canonical(args),'application/json',trust='worker')
        # Freeze generated effect IDs/timestamps once so a repeat is byte-identical.
        with self.store.tx() as db:
            try:h=self.store.head(scope,'native-tool-call',call_id,db=db)
            except RuntimeFault as exc:
                if exc.code!='NOT_FOUND':raise
                h=None
            if h:
                if h['data']['request_digest']!=request_digest or h['data']['worker_id']!=worker.subject_id:
                    raise Conflict('NATIVE_CALL_CONFLICT','Native call ID was reused with different arguments or owner')
                data=h['data']
            else:
                data={'request_digest':request_digest,'worker_id':worker.subject_id,'identity':identity,
                      'effect_id':new_id('effect'),'requested_at':now(),'tool_ref':self.bindings[name],
                      'effect_key':'amplai:'+call_id,'name':name,'args_digest':digest(args),'args_artifact':artifact}
                self.store.cas(db,scope,'native-tool-call',call_id,0,'bound',data)
                self.store.event(db,scope,'run',envelope['run_id'],'tool.native_bound',{'call_id':call_id,'identity':identity})
        artifact=data['args_artifact']
        request={'schema_version':'3.0.0','scope':scope.wire(),'effect_id':data['effect_id'],'run_id':envelope['run_id'],
                 'contract_ref':envelope['contract_ref'],'graph_ref':envelope['graph_ref'],'lease_id':envelope['lease_id'],
                 'fencing_token':envelope['fencing_token'],'tool_ref':data['tool_ref'],'effect_class':tool.effect_class,
                 'args_artifact':artifact,'effect_key':data['effect_key'],'grant_ref':envelope['grant_ref'],'requested_at':data['requested_at']}
        prepared=self.effects.prepare(worker,request)
        receipt=self.effects.dispatch(scope,data['effect_id'])
        return {'native_call_id':native_call_id,'native_turn_id':native_turn_id,'session_handle':session_handle,
                'effect_id':data['effect_id'],'receipt':receipt,'goal_verified':False}

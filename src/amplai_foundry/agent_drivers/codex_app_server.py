"""Stable Codex App Server JSONL transport with exact thread/turn correlation.

Experimental process/* and thread/shellCommand are deliberately unavailable. All
native code runs inside the configured container. Native acceptance never changes
an AMPLAI contract or bypasses its independent verifier.
"""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .protocol import JsonlDecoder, SessionJournal
from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.errors import Hold, RuntimeFault


class JsonlRpc:
    """One bounded JSONL connection; request IDs are scoped to this connection."""

    def __init__(self, process, on_notification: Callable, *,timeout=30):
        self.process = process
        self.notify = on_notification
        self.timeout = timeout
        self.decoder = JsonlDecoder()
        self.sequence = 0
        self.pending = {}
        self.lock = threading.RLock()
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)
        self.selector.register(process.stderr, selectors.EVENT_READ)
        self.stderr_bytes = 0
        self.failure = None

    def send(self, value):
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()+b'\n'
        if len(encoded)>1024*1024:
            raise Hold('RPC_REQUEST_LIMIT', 'Native RPC request exceeds its byte budget')
        try:
            self.process.stdin.write(encoded)
            self.process.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise Hold('RPC_DISCONNECTED', 'Native request outcome is unknown after disconnect') from exc

    def _pump(self, seconds):
        for key, _ in self.selector.select(max(0, seconds)):
            data=os.read(key.fileobj.fileno(), 16384)
            if not data:
                self.selector.unregister(key.fileobj)
                if key.fileobj is self.process.stdout:
                    self.decoder.feed(b'', final=True)
                    raise Hold('RPC_DISCONNECTED','Native process ended without a correlated result')
                continue
            if key.fileobj is self.process.stderr:
                self.stderr_bytes+=len(data)
                if self.stderr_bytes>4*1024*1024:
                    raise Hold('RPC_STDERR_LIMIT','Native stderr exceeded its byte budget')
                continue  # never persist raw credential or reasoning text
            for value in self.decoder.feed(data):
                if 'method' in value:
                    if 'id' in value:
                        method=value['method']
                        if method in {'item/commandExecution/requestApproval','item/fileChange/requestApproval'}:
                            self.send({'id':value['id'],'result':{'decision':'decline'}})
                            self.notify('approval.declined', {'method':method})
                        else:
                            self.send({'id':value['id'],'error':{'code':-32601,'message':'Server request not admitted by AMPLAI'}})
                            raise Hold('RPC_SERVER_REQUEST','Unqualified native server request was denied')
                    else:
                        self.notify(value['method'], value.get('params',{}))
                elif 'id' in value and value['id'] in self.pending:
                    if self.pending[value['id']] is not None:
                        raise Hold('RPC_DUPLICATE_RESPONSE','Native response ID was reused')
                    self.pending[value['id']]=value
                else:
                    raise Hold('RPC_CALL_MAPPING','Unknown response ID cannot be attached to another call')

    def request(self, method, params):
        with self.lock:
            self.sequence+=1;request_id='amplai-'+str(self.sequence)
            self.pending[request_id]=None
            self.send({'id':request_id,'method':method,'params':params})
            deadline=time.monotonic()+self.timeout
            try:
                while self.pending[request_id] is None:
                    remaining=deadline-time.monotonic()
                    if remaining<=0: raise Hold('RPC_TIMEOUT','Native request outcome is unknown; do not resend blindly')
                    self._pump(remaining)
                response=self.pending[request_id]
                if 'error' in response:
                    raise Hold('RPC_REJECTED','Native server rejected the correlated request',details={'method':method,'code':response['error'].get('code')})
                if 'result' not in response: raise Hold('RPC_RESULT','Native response has no result')
                return response['result']
            finally:
                self.pending.pop(request_id,None)

    def poll(self, seconds=0):
        with self.lock:
            self._pump(seconds)

    def close(self):
        self.selector.close()


class CodexAppServerDriver:
    NOTIFICATIONS=frozenset({
        'thread/started','thread/status/changed','thread/tokenUsage/updated',
        'turn/started','turn/completed','turn/diff/updated','turn/plan/updated',
        'item/started','item/completed','item/agentMessage/delta',
        'item/reasoning/summaryTextDelta','item/reasoning/textDelta',
        'item/reasoning/summaryPartAdded','item/commandExecution/outputDelta',
        'item/fileChange/outputDelta','item/mcpToolCall/progress',
        'serverRequest/resolved','error','approval.declined',
    })

    def __init__(self, version, sandbox, journal: SessionJournal, *,model,
                 qualified=False, environment=None, rpc_timeout=30):
        if not model or model in {'auto','latest','default'}:
            raise Hold('MODEL_UNPINNED','An exact configured model is required')
        self.version,self.sandbox,self.journal=version,sandbox,journal
        self.model,self.qualified,self.environment=model,qualified,environment or {}
        self.rpc_timeout=rpc_timeout;self.connections={};self.processes={}

    def probe(self):
        return {'driver_id':'codex-app-server','driver_version':self.version,
                'maturity':'qualified' if self.qualified else 'experimental',
                'native_steering':True,'experimental_api':False,'native_delegation':False}

    def prepare(self, dispatch, prompt, workspace, *,session=None, native_home=None):
        if not self.qualified: raise Hold('DRIVER_UNQUALIFIED','Exact App Server protocol/environment qualification is required')
        if session in {'latest','continue'}: raise Hold('SESSION_UNPINNED','Only an explicit thread can be resumed')
        did=dispatch['dispatch_id'];home=Path(native_home) if native_home else self.journal.root/'native-state'/did
        if native_home and not home.absolute().is_relative_to(self.journal.root.absolute()/'native-state'):
            raise Hold('SESSION_HOME','Native state is outside this driver-owned session directory')
        home.mkdir(parents=True,exist_ok=True,mode=0o700)
        command=self.sandbox.command(['codex','app-server'],Path(workspace),did,
            env_names=list(self.environment),interactive=True,native_home=home)
        self.journal.create(did,{'dispatch_digest':digest(dispatch),'prompt_digest':digest(prompt),
            'model':self.model,'version':self.version,'session':session,'native_home':str(home)})
        return {'dispatch_id':did,'command':command,'prompt':prompt,'session':session,'native_home':str(home)}

    def _notification(self, did, method, params):
        if method not in self.NOTIFICATIONS:
            raise Hold('UNKNOWN_PROVIDER_EVENT','App Server notification requires protocol qualification: '+method)
        record=self.journal.read(did)
        thread_id=params.get('threadId')
        if thread_id and record.get('session_handle') and thread_id!=record['session_handle']:
            raise Hold('RPC_SESSION_MISMATCH','Native event belongs to another thread')
        turn=params.get('turn',{})
        turn_id=params.get('turnId') or turn.get('id')
        if turn_id and record.get('turn_id') and turn_id!=record['turn_id']:
            raise Hold('RPC_TURN_MISMATCH','Native event belongs to another turn')
        changes={}
        if method=='thread/started':
            native=params.get('thread',{}).get('id')
            if native and record.get('session_handle') not in {None,native}:
                raise Hold('RPC_SESSION_MISMATCH','Native server created an unexpected thread')
            if native: changes['session_handle']=native
        if method=='turn/started' and turn_id: changes['turn_id']=turn_id
        if method=='turn/completed':
            state={'completed':'completed','interrupted':'interrupted','failed':'failed'}.get(turn.get('status'))
            if not state: raise Hold('RPC_TURN_STATUS','Unknown turn completion status')
            changes['state']=state
        if method=='thread/tokenUsage/updated':
            changes['usage']=params.get('tokenUsage',{})
        if method=='error': changes['state']='failed'
        event={'method':method,'event_digest':digest(params),'turn_id':turn_id}
        changes['events']=[*record.get('events',[]),event][-4096:]
        # Only digests/IDs/usage are retained, not private reasoning or raw tool data.
        self.journal.update(did,**changes)

    def start(self, prepared):
        did=prepared['dispatch_id'];record=self.journal.read(did)
        if record['state']!='prepared':
            if did not in self.connections and record['state'] in {'starting','running'}:
                raise Hold('RPC_ORPHAN','Reconcile the existing named native process before restart')
            return did
        self.journal.update(did,state='starting',native_home=prepared['native_home'])
        env={k:os.environ[k] for k in ('PATH','LANG','LC_ALL') if k in os.environ};env.update(self.environment)
        try:
            process=subprocess.Popen(prepared['command'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,env=env,start_new_session=True,bufsize=0)
        except OSError as exc:
            self.journal.update(did,state='failed',failure='spawn_failure')
            raise Hold('RPC_SPAWN','App Server container could not start') from exc
        self.processes[did]=process
        rpc=JsonlRpc(process,lambda method,params:self._notification(did,method,params),timeout=self.rpc_timeout)
        self.connections[did]=rpc;self.journal.update(did,pid=process.pid)
        try:
            rpc.request('initialize',{'clientInfo':{'name':'amplai','title':'AMPLAI V3','version':'3.0.0'},'capabilities':{'experimentalApi':False}})
            rpc.send({'method':'initialized','params':{}})
            if prepared['session']:
                result=rpc.request('thread/resume',{'threadId':prepared['session']})
            else:
                result=rpc.request('thread/start',{'model':self.model,'cwd':'/workspace','approvalPolicy':'never','sandbox':'workspace-write'})
            session=result.get('thread',{}).get('id')
            if not session or (prepared['session'] and session!=prepared['session']):
                raise Hold('RPC_SESSION_MISMATCH','Resume did not return the exact requested thread')
            self.journal.update(did,session_handle=session,state='running')
            result=rpc.request('turn/start',{'threadId':session,'input':[{'type':'text','text':prepared['prompt']}]})
            turn_id=result.get('turn',{}).get('id')
            if not turn_id: raise Hold('RPC_TURN_ID','Native start did not return a turn ID')
            self.journal.update(did,turn_id=turn_id)
            return did
        except Exception as exc:
            self.journal.update(did,state='unknown',failure=getattr(exc,'code',type(exc).__name__))
            raise

    def poll(self, handle):
        if handle not in self.connections: raise Hold('RPC_ORPHAN','Exact native connection needs reconciliation')
        record=self.journal.read(handle)
        if record['state'] in {'starting','running'}:
            self.connections[handle].poll(0)
        return self.journal.read(handle)

    def steer(self, handle, event):
        record=self.journal.read(handle)
        if record['state']!='running': raise Hold('RPC_NOT_RUNNING','Native steering needs an active turn')
        result=self.connections[handle].request('turn/steer',{'threadId':record['session_handle'],
            'expectedTurnId':record['turn_id'],'input':[{'type':'text','text':event['text']}]})
        if result.get('turnId')!=record['turn_id']: raise Hold('RPC_TURN_MISMATCH','Steering acknowledged another turn')
        return {'status':'queued','native_turn_id':record['turn_id'],'native_acknowledged':True,'native_applied':False}

    def cancel(self, handle):
        record=self.journal.read(handle)
        if record['state']=='running':
            self.connections[handle].request('turn/interrupt',{'threadId':record['session_handle'],'turnId':record['turn_id']})
        self.sandbox.stop(handle)
        try: self.processes[handle].wait(timeout=15)
        except subprocess.TimeoutExpired as exc: raise Hold('CANCEL_UNCONFIRMED','Native process remains alive') from exc
        self.journal.update(handle,state='cancelled')
        return {'process_stopped':True,'session_handle':record['session_handle']}

    pause=cancel

    def checkpoint(self, handle):
        record=self.journal.read(handle)
        if record['state'] not in {'completed','cancelled','interrupted','failed'}:
            raise Hold('CHECKPOINT_UNCONFIRMED','Native process/turn boundary is not confirmed')
        return {'session_handle':record['session_handle'],'driver_version':self.version,
                'model':self.model,'native_home':record['native_home'],'journal_digest':digest(record)}

    def resume(self, dispatch, prompt, workspace, checkpoint):
        if checkpoint['driver_version']!=self.version or checkpoint['model']!=self.model:
            raise Hold('RESUME_PROFILE','Model/protocol changed since checkpoint')
        return self.start(self.prepare(dispatch,prompt,workspace,session=checkpoint['session_handle'],native_home=checkpoint['native_home']))

    def collect(self, handle):
        record=self.poll(handle)
        if record['state']!='completed': raise Hold('DRIVER_NOT_COMPLETE','Native turn is not complete')
        # A completed turn can still leave child terminals. Enforce a container boundary.
        self.sandbox.stop(handle)
        self.processes[handle].wait(timeout=15)
        return {'provider_completed':True,'goal_verified':False,'process_stopped':True,
                'session_handle':record['session_handle'],'usage':record.get('usage')}

    def destroy(self, handle):
        if not self.sandbox.stopped(handle): raise Hold('DESTROY_RUNNING','A live native process cannot be forgotten')
        if handle in self.connections: self.connections.pop(handle).close()
        self.processes.pop(handle,None);self.sandbox.destroy(handle)

from dataclasses import replace
import hashlib
import json
import pytest
from fastapi.testclient import TestClient
from amplai_foundry.control_plane.api_v3.server import ApiServices, BearerAuthenticator, create_app, ApiCommands
from amplai_foundry.runtime.errors import RuntimeFault, Hold
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.storage.store import Scope

TOKEN='test-only-api-token-aaaaaaaaaaaaaaaaaaaaaaaa'

@pytest.fixture
def api(deployment):
    d=deployment
    actors={'reader':replace(d.actor,permissions=d.actor.permissions|{'runtime.read'}),'other':Actor('other-user',Scope('other','project'),frozenset({'runtime.read','goal.submit'}),authn_context_ref='test-only')}
    bindings={hashlib.sha256(TOKEN.encode()).hexdigest():'reader'}
    authenticator=BearerAuthenticator(bindings,lambda b:actors[b])
    services=ApiServices(d.runtime,d.goals,authenticator,verification=d.verification)
    with TestClient(create_app(services)) as c:
        c.headers['Authorization']='Bearer '+TOKEN
        yield d,c,actors,services


def test_authentication_scope_not_from_body(api):
    d,c,actors,_=api
    assert c.get('/api/v3/goals',headers={'Authorization':'invalid'}).status_code==401
    response=c.post('/api/v3/intents',headers={'Idempotency-Key':'i1'},json={'text':'Cortex repair','scope':{'tenant_id':'other','project_id':'project'}})
    assert response.status_code==422
    response=c.post('/api/v3/intents',headers={'Idempotency-Key':'i2'},json={'text':'Cortex repair'})
    assert response.status_code==202
    goal=response.json()['goal_id']
    assert c.get('/api/v3/goals/'+goal).status_code==200
    actors['reader']=actors['other']
    assert c.get('/api/v3/goals/'+goal).status_code==404


def test_api_replay_conflict_and_no_automatic_execution(api):
    d,c,actors,_=api
    kwargs={'headers':{'Idempotency-Key':'delivery-42'},'json':{'text':'Make Cortex reliable'}}
    first=c.post('/api/v3/intents',**kwargs)
    again=c.post('/api/v3/intents',**kwargs)
    assert first.status_code==again.status_code==202
    assert first.json()==again.json()
    assert c.post('/api/v3/intents',headers=kwargs['headers'],json={'text':'Different goal'}).status_code==409
    assert d.store.head(d.scope,'goal',first.json()['goal_id'])['state']=='draft'
    assert d.store.conn.execute('SELECT COUNT(*) FROM leases').fetchone()[0]==0


def test_api_current_permissions_rechecked_on_replay(api):
    d,c,actors,_=api
    kwargs={'headers':{'Idempotency-Key':'revoke'},'json':{'text':'A bounded task'}}
    assert c.post('/api/v3/intents',**kwargs).status_code==202
    actors['reader']=replace(actors['reader'],permissions=frozenset())
    assert c.post('/api/v3/intents',**kwargs).status_code==403


def test_body_limit_and_strict_protocol(api):
    d,c,_,_=api
    assert c.post('/api/v3/intents',json={'text':'x'}).status_code==400
    assert c.post('/api/v3/intents',headers={'Idempotency-Key':'s'},json={'text':1}).status_code==422
    assert c.post('/api/v3/intents',headers={'Idempotency-Key':'b'},content=b'x'*(4*1024*1024+1)).status_code==413


def test_api_etag_and_stale_steering(api):
    d,c,_,_=api
    p=d.prepare()
    response=c.get('/api/v3/goals/'+p['goal_id'])
    assert response.headers['etag']=='"'+str(response.json()['row_version'])+'"'
    body={'kind':'pause','text':'Pause at a safe boundary','expected_contract_ref':p['contract_ref']}
    assert c.post('/api/v3/goals/'+p['goal_id']+'/steering',headers={'Idempotency-Key':'bad'},json=body).status_code==400
    response=c.post('/api/v3/goals/'+p['goal_id']+'/steering',headers={'Idempotency-Key':'stale','If-Match':'"999"'},json=body)
    assert response.status_code==409


def test_worker_cannot_declare_process_stopped(api):
    d,c,_,_=api
    p=d.prepare(); dispatch=d.runtime.claim(d.worker,goal_id=p['goal_id'])
    # The request schema has no process_stopped or usage override.
    body={'lease_id':dispatch['lease']['lease_id'],'fence':dispatch['lease']['fencing_token'],'process_stopped':True}
    response=c.post('/api/v3/runs/'+dispatch['run_id']+'/collect',headers={'Idempotency-Key':'collect'},json=body)
    assert response.status_code==422


def test_api_events_resume_are_scoped(api):
    d,c,actors,_=api
    result=c.post('/api/v3/intents',headers={'Idempotency-Key':'event'},json={'text':'event creation'})
    assert result.status_code==202
    response=c.get('/api/v3/events')
    assert response.status_code==200 and 'intent.submitted' in response.text
    last=d.store.events(d.scope)[-1]['seq']
    assert 'intent.submitted' not in c.get('/api/v3/events',headers={'Last-Event-ID':str(last)}).text
    actors['reader']=actors['other']
    assert result.json()['goal_id'] not in c.get('/api/v3/events').text


def test_api_inflight_crash_is_not_reexecuted(deployment):
    d=deployment;service=ApiCommands(d.store);count=[]
    def crash():count.append(1);raise ConnectionError('transport interrupted')
    with pytest.raises(ConnectionError):service.run(d.actor,'key','effect',{'x':1},crash)
    with pytest.raises(Hold,match='in flight'):service.run(d.actor,'key','effect',{'x':1},crash)
    assert count==[1]


def test_api_cannot_register_grant_or_fake_qualification(api):
    _,c,_,_=api
    body={'value':{'status':'pass'},'object_id':'forged','revision':1}
    for kind in ['execution-grant','qualification-report','verdict','attestation','release-set']:
        response=c.post('/api/v3/registry/'+kind,headers={'Idempotency-Key':kind},json=body)
        assert response.status_code==400 and response.json()['code']=='PROTECTED_REGISTRY'

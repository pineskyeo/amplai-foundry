"""DEV-03 evolution gate, concurrency, target binding and rollback tests."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest
from fastapi.testclient import TestClient

from amplai_foundry.control_plane.api_v3.server import ApiServices, create_app
from amplai_foundry.meta_harness.pipeline_reference import PipelineMetaReference
from amplai_foundry.meta_harness.reference import MetaReference
from amplai_foundry.runtime.contracts.identity import canonical, digest, new_id
from amplai_foundry.runtime.errors import Hold, RuntimeFault, Conflict
from amplai_foundry.evaluation.observatory import Observatory


@pytest.fixture
def meta03(tmp_path):
    m=MetaReference(tmp_path/'meta')
    try:yield m
    finally:m.close()


def ready(m, overrides=None):
    p=m.prepare();pid=p['proposal_id']
    m.meta.start_offline(m.reviewer,pid)
    report=m.eval.run(m.reviewer,p['experiment_ref'],m.execute_case)
    m.meta.evaluate(m.reviewer,pid,report)
    mapping={c['case_id']:m.canary_target(c['case_id']) for c in p['cases'][:2]}
    policy={'policy_id':new_id('canary-policy'),'eligible_task_ids':list(mapping),'max_runs':2,
        'max_wall_seconds':120,'max_cost_microunits':100,'max_trial_cost_microunits':10,
        'max_trial_tokens':0,'max_concurrent':1,'project_opt_in':True,'eligible_risk_classes':['low'],
        'target_binding_refs':list({digest(r):r for r in mapping.values()}.values()),'task_binding_refs':mapping,
        'abort_on_safety_failure':True,'abort_on_unknown_effect':True,'fallback_release_ref':p['baseline_release_ref'],
        **(overrides or {})}
    return {**p,'report_ref':report,'policy':policy}


def start(m,p):
    pr=m.put('canary-policy',p['policy'])
    approval=m.approve('canary.execute',digest({'proposal_ref':p['proposal_ref'],'report_ref':p['report_ref'],'policy_ref':pr}))
    m.meta.approve_canary(m.reviewer,p['proposal_id'],pr,approval)
    m.meta.start_canary(m.reviewer,p['proposal_id'])
    return {**p,'policy_ref':pr,'canary_approval_ref':approval}


def result(m,p,task):
    c=next(c for c in p['cases'] if c['case_id']==task)
    obs=m.execute_case(p['candidate_ref'],c,0,'canary')
    return {'success':obs.success,'safety_failures':obs.safety_failures,'unknown_effects':obs.unknown_effects,
        'cost_microunits':obs.cost_microunits,'input_tokens':obs.input_tokens,'output_tokens':obs.output_tokens,
        'artifact_ref':obs.artifact_refs[0]}


def promotion_plan(m,p):
    value={'schema_version':'3.0.0','promotion_id':new_id('promotion'),'scope':m.d.scope.wire(),
        'expected_active_release_ref':p['baseline_release_ref'],'candidate_release_ref':p['candidate_release_ref'],
        'eval_report_ref':p['report_ref'],'target_binding_refs':p['policy']['target_binding_refs'],
        'canary_policy_ref':p['policy_ref'],'abort_rules':['Abort unknown effects','Abort safety failure'],
        'rollback_release_ref':p['baseline_release_ref'],
        'expires_at':(datetime.fromtimestamp(m.d.store.clock(),timezone.utc)+timedelta(minutes=5)).isoformat().replace('+00:00','Z'),
        'active_run_policy':'drain'}
    value['grant_ref']=m.approve('release.promote',digest(value))
    return value


def complete_canary(m):
    p=start(m,ready(m))
    for task in p['policy']['eligible_task_ids']:
        m.meta.canary_trial(m.reviewer,p['proposal_id'],task,lambda t:result(m,p,t))
    m.meta.request_promotion(m.reviewer,p['proposal_id'])
    return p


@pytest.mark.parametrize('overrides', [
    {'project_opt_in':False}, {'project_opt_in':1}, {'eligible_risk_classes':['high']},
    {'max_concurrent':0}, {'max_runs':True}, {'max_trial_cost_microunits':None},
    {'abort_on_safety_failure':False}, {'abort_on_unknown_effect':False}, {'max_runs':3},
    {'eligible_task_ids':['same','same']}, {'target_binding_refs':[]}, {'task_binding_refs':{}},
])
def test_dev03_canary_requires_bounded_opt_in_policy(meta03,overrides):
    p=ready(meta03,overrides)
    with pytest.raises(RuntimeFault):start(meta03,p)
    assert meta03.d.store.head(meta03.d.scope,'evolution',p['proposal_id'])['state']=='offline_evaluated'


def test_dev03_canary_reserves_before_io_and_reentrant_admission_is_blocked(meta03):
    m=meta03;p=start(m,ready(m));tasks=p['policy']['eligible_task_ids'];inner=[]
    def callback(t):
        h=m.d.store.head(m.d.scope,'evolution',p['proposal_id'])
        assert t in h['data']['canary_pending']
        assert m.meta.budgets.totals(m.d.scope,p['proposal_id'])['pending']==1
        with pytest.raises(Hold) as e:
            m.meta.canary_trial(m.reviewer,p['proposal_id'],tasks[1],lambda x:inner.append(x))
        assert e.value.code=='CANARY_CONCURRENCY'
        return result(m,p,t)
    m.meta.canary_trial(m.reviewer,p['proposal_id'],tasks[0],callback)
    assert inner==[]
    assert m.meta.budgets.totals(m.d.scope,p['proposal_id'])['pending']==0
    with pytest.raises(Hold):m.meta.canary_trial(m.reviewer,p['proposal_id'],tasks[0],callback)


def test_dev03_two_real_threads_cannot_overbook_canary_slot(meta03):
    m=meta03;p=start(m,ready(m));tasks=p['policy']['eligible_task_ids']
    begun,release=threading.Event(),threading.Event()
    def block(t):
        begun.set()
        assert release.wait(5), 'test synchronization timeout'
        return result(m,p,t)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f=pool.submit(m.meta.canary_trial,m.reviewer,p['proposal_id'],tasks[0],block)
        assert begun.wait(5)
        try:
            with pytest.raises(Hold):m.meta.canary_trial(m.reviewer,p['proposal_id'],tasks[1],lambda t:pytest.fail('unexpected dispatch'))
        finally:release.set()
        assert f.result(timeout=10)['state']=='canary_running'


def test_dev03_canary_cost_reserved_before_first_callback(meta03):
    m=meta03;p=start(m,ready(m,{'max_trial_cost_microunits':101}));calls=[]
    with pytest.raises(Hold) as e:m.meta.canary_trial(m.reviewer,p['proposal_id'],p['cases'][0]['case_id'],calls.append)
    assert e.value.code=='CANARY_COST' and calls==[]


@pytest.mark.parametrize('attack', ['boolean_counter','wrong_target','wrong_risk','wrong_scope','foreign_task','missing_receipt','wrong_composition'])
def test_dev03_canary_observation_must_bind_authorized_work(meta03,attack):
    m=meta03;p=start(m,ready(m));task=p['cases'][0]['case_id']
    def callback(t):
        r=result(m,p,t);proof=json.loads(m.d.artifacts.read(m.d.scope,r['artifact_ref']))
        if attack=='boolean_counter':r['cost_microunits']=False
        if attack=='wrong_target':proof['target_binding_ref']={'id':'foreign','revision':1,'digest':'sha256:'+'0'*64}
        if attack=='wrong_risk':proof['risk_class']='high'
        if attack=='wrong_scope':proof['scope']={'tenant_id':'other','project_id':'project'}
        if attack=='foreign_task':proof['task_id']='other'
        if attack=='wrong_composition':proof['composition_ref']=p['baseline_ref']
        r['artifact_ref']=m.d.artifacts.admit(m.d.scope,canonical(proof),'application/json',trust='verifier')
        if attack=='missing_receipt':r.pop('artifact_ref')
        return r
    r=m.meta.canary_trial(m.reviewer,p['proposal_id'],task,callback)
    assert r['state']=='aborted'
    assert m.meta.budgets.totals(m.d.scope,p['proposal_id'])['unknown']==1
    with pytest.raises(Hold):m.meta.request_promotion(m.reviewer,p['proposal_id'])


@pytest.mark.parametrize('action', ['revoke','kill'])
def test_dev03_admission_authority_rechecked_after_canary_callback(meta03,action):
    m=meta03;p=start(m,ready(m));task=p['cases'][0]['case_id']
    def callback(t):
        r=result(m,p,t)
        if action=='revoke':m.approvals[digest(p['canary_approval_ref'])]['revoked']=True
        else:m.meta.kill_switch(m.reviewer,True,'incident during callback')
        return r
    r=m.meta.canary_trial(m.reviewer,p['proposal_id'],task,callback)
    assert r['state']=='aborted'
    assert m.d.store.head(m.d.scope,'release-pointer','active')['data']['release_ref']==p['baseline_release_ref']


def test_dev03_crashed_canary_does_not_resume_old_owner(meta03):
    m=meta03;p=start(m,ready(m));task=p['cases'][0]['case_id']
    def crash(t):raise KeyboardInterrupt('process died during dispatch')
    with pytest.raises(KeyboardInterrupt):m.meta.canary_trial(m.reviewer,p['proposal_id'],task,crash)
    m.d.store.epoch+=1
    with pytest.raises(Hold):m.meta.canary_trial(m.reviewer,p['proposal_id'],task,lambda t:pytest.fail('replayed'))
    actor=replace(m.reviewer,permissions=m.reviewer.permissions|{'experiment.reconcile'})
    assert m.meta.recover_canary(actor,p['proposal_id'])['state']=='aborted'
    assert m.meta.budgets.totals(m.d.scope,p['proposal_id'])['pending']==1


def test_dev03_contamination_after_evaluation_prevents_canary(meta03):
    m=meta03;p=ready(m);plan=m.d.store.get(m.d.scope,'eval-experiment',p['experiment_ref'])
    m.eval.corpus.mark_contaminated(m.reviewer,plan['corpus_ref'],'later discovered exposure')
    with pytest.raises(Hold):start(m,p)


def test_dev03_forged_report_cannot_replace_the_recorded_experiment_output(meta03):
    m=meta03;p=ready(m);report=m.d.store.get(m.d.scope,'eval-report',p['report_ref'])
    forged={**report,'report_id':new_id('forged-report')}
    ref=m.put('eval-report',forged)
    with pytest.raises(Hold) as e:m.meta._passing_report(m.d.scope,ref)
    assert e.value.code=='REPORT_PROVENANCE'


@pytest.mark.parametrize('path', ['src/amplai_foundry/evaluation/service.py','src/amplai_foundry/meta_harness/budget.py',
    'src/amplai_foundry/governance/service.py','eval/holdout/tasks.json','contracts/state-machines.json',
    '../runtime.py','/tmp/runtime.py','a/../b','a\\b'])
def test_dev03_meta_changes_cannot_modify_their_own_protection(meta03,path):
    m=meta03;p=m.prepare();base=m.d.store.get(m.d.scope,'harness-change-proposal',p['proposal_ref'])
    change=m.d.artifacts.admit(m.d.scope,canonical({'baseline_ref':base['baseline_ref'],'candidate_ref':base['candidate_ref'],
        'changed_paths':[path]}),'application/json',trust='operator')
    proposal={**base,'proposal_id':new_id('screen-test'),'change_artifact':change}
    m.meta.submit(m.proposer,proposal)
    with pytest.raises(RuntimeFault):m.meta.screen(m.reviewer,proposal['proposal_id'])


def test_dev03_proposer_cannot_reenable_killed_runtime(meta03):
    m=meta03;m.meta.kill_switch(m.reviewer,True,'operator incident')
    proposer=replace(m.proposer,permissions=m.proposer.permissions|{'runtime.admin'})
    with pytest.raises(Hold):m.meta.kill_switch(proposer,False,'self recovery')
    assert m.d.store.head(m.d.scope,'runtime-control','kill')['data']['enabled']


@pytest.mark.parametrize('attack',['targets','expired','different_fallback','different_active'])
def test_dev03_promotion_binds_exact_targets_expiry_and_current_pointer(meta03,attack):
    m=meta03;p=complete_canary(m);plan=promotion_plan(m,p)
    if attack=='targets':plan['target_binding_refs']=[]
    if attack=='expired':plan['expires_at']='2020-01-01T00:00:00Z'
    if attack=='different_fallback':plan['rollback_release_ref']=p['candidate_release_ref']
    if attack=='different_active':
        with m.d.store.tx() as db:
            h=m.d.store.head(m.d.scope,'release-pointer','active',db=db)
            m.d.store.cas(db,m.d.scope,'release-pointer','active',h['row_version'],'active',{'release_ref':p['candidate_release_ref']})
    with pytest.raises(RuntimeFault):m.meta.promote(m.reviewer,p['proposal_id'],plan)


def test_dev03_release_key_revocation_blocks_validly_signed_candidate(meta03):
    m=meta03;p=complete_canary(m);plan=promotion_plan(m,p)
    m.meta.keys.clear()
    with pytest.raises(RuntimeFault):m.meta.promote(m.reviewer,p['proposal_id'],plan)


def test_dev03_rollback_never_restores_budget_or_grants(meta03):
    m=meta03;p=complete_canary(m);plan=promotion_plan(m,p)
    before=m.meta.budgets.totals(m.d.scope,p['proposal_id'])
    grants=m.d.store.conn.execute('SELECT COUNT(*) FROM grant_uses').fetchone()[0]
    m.meta.promote(m.reviewer,p['proposal_id'],plan)
    approval=m.approve('release.rollback',digest({'target_ref':p['baseline_release_ref'],'expected_active_ref':p['candidate_release_ref']}))
    result_value=m.meta.rollback(m.reviewer,p['proposal_id'],p['baseline_release_ref'],p['candidate_release_ref'],approval)
    assert result_value['authority_restored'] is False
    assert before==m.meta.budgets.totals(m.d.scope,p['proposal_id'])
    assert grants==m.d.store.conn.execute('SELECT COUNT(*) FROM grant_uses').fetchone()[0]


def test_dev03_rollback_cannot_claim_unknown_effect_was_undone(meta03):
    m=meta03;p=complete_canary(m);m.meta.promote(m.reviewer,p['proposal_id'],promotion_plan(m,p))
    with m.d.store.tx() as db:m.d.store.cas(db,m.d.scope,'effect','unknown-fixture',0,'unknown',{})
    approval=m.approve('release.rollback',digest({'target_ref':p['baseline_release_ref'],'expected_active_ref':p['candidate_release_ref']}))
    with pytest.raises(Hold):m.meta.rollback(m.reviewer,p['proposal_id'],p['baseline_release_ref'],p['candidate_release_ref'],approval)
    assert m.d.store.head(m.d.scope,'release-pointer','active')['data']['release_ref']==p['candidate_release_ref']


def test_dev03_api_no_client_executor_or_result_injection(meta03):
    m=meta03;p=m.prepare();actor=replace(m.reviewer,permissions=m.reviewer.permissions|{'runtime.read','experiment.read'})
    services=ApiServices(m.d.runtime,m.d.goals,lambda _:actor,meta=m.meta,evaluation=m.eval)
    with TestClient(create_app(services)) as client:
        headers={'Idempotency-Key':'no-injection'}
        r=client.post('/api/v3/evaluation/run',headers=headers,json={'ref':p['experiment_ref'],'success':True})
        assert r.status_code==422
        r=client.post('/api/v3/evaluation/run',headers=headers,json={'ref':p['experiment_ref']})
        assert r.status_code>=400
        params={'revision':p['experiment_ref']['revision'],'digest':p['experiment_ref']['digest']}
        assert client.get('/api/v3/objects/eval-experiment/'+p['experiment_ref']['id'],params=params).status_code==200
        services.authenticate=lambda _:replace(actor,permissions=actor.permissions|{'harness.propose'})
        assert client.get('/api/v3/objects/eval-experiment/'+p['experiment_ref']['id'],params=params).status_code>=400


@pytest.mark.parametrize('bad',[False,True])
def test_dev03_actual_pipeline_uses_frozen_composition_and_independent_results(tmp_path,bad):
    m=PipelineMetaReference(tmp_path/'pipeline')
    try:
        p=m.prepare(bad_candidate=bad);value=m.execute(p)
        assert value['verdict']==('fail' if bad else 'pass')
        assert value['actual_pipeline_trials']==(48 if bad else 50)
        for trial in m.case_runs:
            run=m.d.store.head(m.d.scope,'run',trial['run_id'])['data']['record']
            assert run['composition_ref']==trial['composition_ref']
            assert run['verdict_refs']
            goal=m.d.store.head(m.d.scope,'goal',trial['goal_id'])
            assert (goal['state']=='verified')==trial['success']
        if not bad:
            assert value['promotion']['state']=='promoted' and value['rollback']['state']=='rolled_back'
            metrics=Observatory(m.d.store).summary(m.d.scope)
            assert metrics['verified_goals']==51  # one infrastructure seed + 50 real trial goals
        else:
            assert value['promoted'] is False
            assert not any(m.d.store.head(m.d.scope,'goal',t['goal_id'])['state']=='active' for t in m.case_runs)
    finally:m.close()

@pytest.mark.parametrize('score', [float('nan'),float('inf'),True,'high'])
def test_dev03_ranking_rejects_ambiguous_scores(meta03,score):
    m=meta03;p=m.prepare()
    with pytest.raises(RuntimeFault):m.meta.compositions.select(m.d.scope,[p['baseline_ref'],p['candidate_ref']],
        classification='internal',required_actions={'workspace.write'},scores={p['candidate_ref']['digest']:score})


def test_dev03_rank_score_cannot_bypass_data_class_policy(meta03):
    m=meta03;p=m.prepare()
    with pytest.raises(Hold):m.meta.compositions.select(m.d.scope,[p['candidate_ref']],classification='restricted',
        required_actions={'workspace.write'},scores={p['candidate_ref']['digest']:999999})


def test_dev03_class_b_needs_independent_human_receipt(meta03):
    m=meta03;p=m.prepare();base=m.d.store.get(m.d.scope,'harness-change-proposal',p['proposal_ref'])
    c=m.d.store.get(m.d.scope,'harness-composition',p['candidate_ref'])
    budget_ref=m.put('budget-policy',{'budget_id':new_id('policy'),'scope':m.d.scope.wire(),'intent':'same-or-lower enforced root limits'})
    new_c={**c,'composition_id':new_id('behavior-candidate'),'budget_policy_ref':budget_ref}
    cr=m.meta.compositions.register(m.proposer,new_c)
    changed=m.d.artifacts.admit(m.d.scope,canonical({'baseline_ref':base['baseline_ref'],'candidate_ref':cr,
        'changed_paths':['configuration/budget-policy.json']}),'application/json',trust='operator')
    prop={**base,'proposal_id':new_id('class-b'),'candidate_ref':cr,'surface_class':'B','change_artifact':changed}
    pref=m.meta.submit(m.proposer,prop)
    with pytest.raises(Hold) as e:m.meta.screen(m.reviewer,prop['proposal_id'])
    assert e.value.code=='CODE_REVIEW_REQUIRED'
    receipt=m.d.artifacts.admit(m.d.scope,canonical({'proposal_ref':pref,'candidate_ref':cr,
        'reviewer_subject_id':m.reviewer.subject_id,'outcome':'pass','protected_controls_changed':False}),
        'application/json',trust='operator')
    with pytest.raises(Hold):m.meta.record_review(replace(m.reviewer,kind='service'),prop['proposal_id'],receipt)
    m.meta.record_review(m.reviewer,prop['proposal_id'],receipt)
    assert m.meta.screen(m.reviewer,prop['proposal_id'])['state']=='screened'


def test_dev03_protected_composition_change_is_not_a_class_a_prompt_edit(meta03):
    m=meta03;p=m.prepare();c=m.d.store.get(m.d.scope,'harness-composition',p['candidate_ref'])
    protected=m.put('verification-policy',{'policy_id':new_id('verify-policy'),'rule':'different protected rule'})
    cr=m.meta.compositions.register(m.proposer,{**c,'composition_id':new_id('protected'),'verification_policy_ref':protected})
    assert m.meta.compositions.classify(m.d.scope,p['baseline_ref'],cr)['surface_class']=='C'


def test_dev03_budget_reconciliation_needs_real_proof_and_independent_identity(meta03):
    m=meta03;p=start(m,ready(m));task=p['cases'][0]['case_id']
    m.meta.canary_trial(m.reviewer,p['proposal_id'],task,lambda _:None)
    h=m.d.store.head(m.d.scope,'meta-budget',p['proposal_id'])
    allocation=next(k for k,v in h['data']['allocations'].items() if v['status']=='unknown')
    receipt=m.d.artifacts.admit(m.d.scope,canonical({'allocation_id':allocation,'tokens':0,'cost_microunits':0,
        'process_stopped':True,'unknown_effects':0}),'application/json',trust='operator')
    reconciler=replace(m.reviewer,permissions=m.reviewer.permissions|{'experiment.reconcile'})
    proposer=replace(m.proposer,permissions=m.proposer.permissions|{'experiment.reconcile'})
    with pytest.raises(Hold):m.meta.budgets.reconcile(proposer,p['proposal_id'],allocation,tokens=0,cost=0,evidence_ref=receipt,artifacts=m.d.artifacts)
    m.meta.budgets.reconcile(reconciler,p['proposal_id'],allocation,tokens=0,cost=0,evidence_ref=receipt,artifacts=m.d.artifacts)
    assert m.meta.budgets.totals(m.d.scope,p['proposal_id'])['unknown']==0
    assert m.d.store.head(m.d.scope,'evolution',p['proposal_id'])['state']=='aborted'
    with pytest.raises(Hold):m.meta.request_promotion(m.reviewer,p['proposal_id'])

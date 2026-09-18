from dataclasses import replace
import json
import pytest
from amplai_foundry.meta_harness.reference import MetaReference
from amplai_foundry.runtime.contracts.identity import canonical,digest,sign,new_id
from amplai_foundry.runtime.errors import Hold,Conflict,RuntimeFault
from amplai_foundry.evaluation.corpus import CorpusService
from amplai_foundry.evaluation.analysis import analyze_pairs

@pytest.fixture
def meta(tmp_path):
    d=MetaReference(tmp_path/'meta')
    try:yield d
    finally:d.close()


def test_real_paired_canary_promote_and_rollback(meta):
    plan=meta.prepare();before=meta.d.store.conn.execute('SELECT COUNT(*) FROM grant_uses').fetchone()[0]
    result=meta.execute(plan)
    assert result['verdict']=='pass'
    assert result['promotion']['state']=='promoted' and result['rollback']['state']=='rolled_back'
    assert result['rollback']['authority_restored'] is False
    assert meta.d.store.conn.execute('SELECT COUNT(*) FROM grant_uses').fetchone()[0]==before
    report=meta.d.store.get(meta.d.scope,'eval-report',result['report_ref'])
    artifact=json.loads(meta.d.artifacts.read(meta.d.scope,report['analysis_artifact'],trusted=True))
    assert len(list((meta.root/'trials').glob('*/result.json')))==50
    assert meta.d.store.head(meta.d.scope,'release-pointer','active')['data']['release_ref']==plan['baseline_release_ref']


def test_bad_candidate_cannot_progress_to_canary(meta):
    p=meta.prepare(bad_candidate=True);result=meta.execute(p)
    assert result['verdict']=='fail' and result['promoted'] is False
    assert meta.d.store.head(meta.d.scope,'release-pointer','active')['data']['release_ref']==p['baseline_release_ref']


def test_static_replay_never_claims_causal_promotion(meta):
    p=meta.prepare(mode='replay');result=meta.execute(p)
    assert result['verdict']=='inconclusive' and result['promoted'] is False


def test_proposer_cannot_approve_even_with_permission(meta):
    p=meta.prepare()
    actor=replace(meta.proposer,permissions=meta.proposer.permissions|{'experiment.approve'})
    plan=meta.d.store.get(meta.d.scope,'eval-experiment',p['experiment_ref'])
    with pytest.raises(Hold) as e:meta.meta.approve_experiment(actor,p['proposal_id'],plan['approval_ref'],p['experiment_ref'])
    assert e.value.code=='SELF_APPROVAL'


def test_kill_switch_stops_actual_runtime_admission(meta):
    meta.meta.kill_switch(meta.reviewer,True,'Test kill propagation')
    with pytest.raises(Hold) as e:meta.d.runtime.claim(meta.d.worker)
    assert e.value.code=='KILL_SWITCH'
    assert meta.d.store.head(meta.d.scope,'runtime-control','kill')['data']['enabled'] is True


def test_release_signature_tampering_detected(meta):
    p=meta.prepare();value=meta.d.store.get(meta.d.scope,'release-set',p['candidate_release_ref'])
    changed={**value,'release_id':new_id('tampered'),'platform_version':'3.0.1'}
    ref=meta.put('release-set',changed)
    with pytest.raises(RuntimeFault):meta.meta._release(meta.d.scope,ref)


def test_holdout_restricted_and_consume_limit(meta):
    service=CorpusService(meta.d.store,meta.d.artifacts)
    artifact=meta.d.artifacts.admit(meta.d.scope,b'{"expected":42}','application/json',trust='operator')
    ref=service.freeze(meta.reviewer,'sealed-test',[{'case_id':'sealed-case','split':'holdout','task_class':'sum','artifact_ref':artifact}],holdout_use_limit=1)
    # Proposer permission cannot turn sealed evaluation data into planning context.
    with pytest.raises(RuntimeFault):service.select(meta.proposer,ref,split='holdout',purpose='evaluation')

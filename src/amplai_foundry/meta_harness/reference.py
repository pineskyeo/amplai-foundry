"""Complete local meta-harness drill on real deterministic arithmetic workspaces.

This qualifies state/control paths, not LLM quality. Independent identities and local
approval receipts are restricted to demo-local; they are not production credentials.
"""
from __future__ import annotations
import json
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
from ..runtime.reference import ReferenceDeployment
from ..runtime.contracts.identity import canonical,digest,new_id,now,sign
from ..runtime.errors import Hold
from ..sandbox.local import DataSandbox
from ..evaluation.corpus import CorpusService
from ..evaluation.service import EvaluationService,TrialObservation,ExecutorPolicy
from .service import MetaHarness

class MetaReference:
    def __init__(self,root):
        self.root=Path(root);self.d=ReferenceDeployment(self.root/'runtime');self.prepared=self.d.prepare();self.d.execute(self.prepared)
        self.proposer=replace(self.d.actor,subject_id='meta-proposer',permissions=frozenset({'harness.propose'}))
        self.reviewer=replace(self.d.actor,subject_id='meta-reviewer',permissions=frozenset({'harness.review','experiment.approve','experiment.run','canary.approve','canary.run','release.promote','release.rollback','runtime.admin','corpus.manage','corpus.read','corpus.holdout.evaluate'}))
        self.approvals={}
        def check(scope,ref,action,subject):
            value=self.approvals.get(digest(ref))
            if scope!=self.d.scope or not value or value['action']!=action or value['subject_digest']!=subject or value.get('revoked'):raise Hold('DEMO_APPROVAL','No exact independent local demo approval')
            return value
        self.check=check
        self.meta=MetaHarness(self.d.store,self.d.contracts,self.d.artifacts,approval_check=check,trusted_release_keys={'demo-release':self.d.signer.public_key()})
        qualification = self.put('executor-qualification', {'qualification_id': new_id('executor-qualification'),
            'status':'pass', 'executor_id':'local-arithmetic-qualification',
            'scope_note':'local deterministic data-only executor; no external model or production qualification'})
        self.eval=EvaluationService(self.d.store,self.d.contracts,self.d.artifacts,approval_check=check,
            executor_id='local-arithmetic-qualification', executor_policy=ExecutorPolicy(
                frozenset({'static','replay','sandbox_rerun','shadow'}),0,0,qualification))
    def put(self,kind,value):
        field=next((k for k in value if k.endswith('_id')),None)
        if not field: value={**value,'object_id':new_id(kind)};field='object_id'
        return self.d.put(kind,value[field],value,value.get('revision',1))
    def approve(self,action,subject):
        value={'approval_id':new_id('demo-approval'),'scope':self.d.scope.wire(),'action':action,'subject_digest':subject,'approved_by':self.reviewer.subject_id,'revoked':False}
        ref=self.put('demo-approval',value);self.approvals[digest(ref)]=value;return ref
    def release(self,name,composition_ref):
        matrix=self.put('qualification-matrix',{'matrix_id':new_id('matrix'),'status':'pass','qualification_scope':'local deterministic data-only reference, not production or external provider','checks':['actual protected file-boundary probes','actual JSON output verification']})
        # Configuration-only reference release: the immutable code descriptor is
        # unchanged. Migration refs point backwards; never backfill a release hash.
        descriptor=self.put('release-inputs',{'inputs_id':new_id('release-inputs'),'platform_version':'3.0.0','code_change':False,'composition_ref':composition_ref})
        inventory=self.put('baseline-inventory',{'inventory_id':new_id('inventory'),'inputs_ref':descriptor})
        backup=self.put('migration-backup',{'backup_id':new_id('backup'),'inputs_ref':descriptor})
        rollback=self.put('rollback-plan',{'rollback_id':new_id('rollback'),'restore_authority':False,'inputs_ref':descriptor})
        migration=self.put('migration-plan',{'schema_version':'3.0.0','migration_id':new_id('migration'),'scope':self.d.scope.wire(),
            'source_release_ref':descriptor,'target_release_ref':descriptor,'baseline_inventory_ref':inventory,
            'operations':[{'operation_id':'keep-code','kind':'keep','path':'release-inputs.json','expected_old_digest':descriptor['digest'],
                'replacement_ref':None,'backup_ref':backup,'required_test_ids':['T-001'],'approval_required':True}],
            'active_run_policy':'drain','rollback_plan_ref':rollback,'dry_run_required':True})
        sbom=self.put('sbom',{'sbom_id':new_id('sbom'),'components':['AMPLAI V3 local reference']})
        provenance=self.put('provenance',{'provenance_id':new_id('provenance'),'test_deployment':True,'source':'running local source tree'})
        release={'schema_version':'3.0.0','release_id':name,'platform_version':'3.0.0','kit_version':'3.0.0','protocol_major':3,'contract_schema_version':'3.0.0',
                 'source_revision':'local-reference','component_refs':[composition_ref],'qualification_matrix_ref':matrix,'migration_plan_ref':migration,
                 'sbom_ref':sbom,'provenance_ref':provenance,'status':'approved'}
        return self.put('release-set',sign(release,'demo-release',self.d.signer))
    def prepare(self, *,mode='sandbox_rerun',bad_candidate=False):
        d=self.d
        baseline=d.store.get(d.scope,'harness-composition',self.prepared['execution_profile']['composition_ref'])
        baseline_prompt=self.put('prompt-bundle',{'prompt_id':new_id('prompt'),'algorithm':'loop_sum'})
        candidate_prompt=self.put('prompt-bundle',{'prompt_id':new_id('prompt'),'algorithm':'wrong_sum' if bad_candidate else 'builtin_sum'})
        b={**baseline,'composition_id':new_id('baseline-composition'),'prompt_bundle_ref':baseline_prompt}
        c={**baseline,'composition_id':new_id('candidate-composition'),'prompt_bundle_ref':candidate_prompt}
        br=self.meta.compositions.register(self.proposer,b);cr=self.meta.compositions.register(self.proposer,c)
        baseline_release=self.release('reference-baseline',br);candidate_release=self.release('reference-candidate',cr)
        with d.store.tx() as db:d.store.cas(db,d.scope,'release-pointer','active',0,'active',{'release_ref':baseline_release})
        draftplan=self.put('experiment-draft',{'draft_id':new_id('exp-draft'),'comparison':'paired actual arithmetic workspaces'})
        rollback=self.put('rollback-plan',{'rollback_id':new_id('rollback'),'target_release_ref':baseline_release})
        observation=d.artifacts.admit(d.scope,b'{"issue":"Exercise actual evolution gates on a controlled arithmetic corpus, not model efficiency claims"}','application/json',trust='verifier')
        observation_ref=self.put('harness-observation',{'observation_id':new_id('observation'),'artifact':observation})
        change=d.artifacts.admit(d.scope,canonical({'changed_paths':['packs/software/prompts/arithmetic.txt'],'baseline_ref':br,'candidate_ref':cr}),'application/json',trust='operator')
        proposal={'schema_version':'3.0.0','proposal_id':new_id('harness-proposal'),'scope':d.scope.wire(),'baseline_ref':br,'candidate_ref':cr,'surface_class':'A',
                  'hypothesis':'In the local reference corpus, builtin summation preserves each arithmetic result. This is not evidence about any LLM.',
                  'observation_refs':[observation_ref],'change_artifact':change,'expected_benefit':'Validate the meta-control lifecycle without changing security boundaries',
                  'risks':['Deterministic toy corpus does not estimate model quality'],'protected_surface_findings':[],'experiment_plan_ref':draftplan,'rollback_plan_ref':rollback,
                  'proposer':self.proposer.wire(),'status':'draft'}
        proposal_ref=self.meta.submit(self.proposer,proposal);self.meta.screen(self.reviewer,proposal['proposal_id'])
        cases=[]
        for i in range(24):
            payload={'numbers':[i,-i//2,2*i+3],'expected':sum([i,-i//2,2*i+3])}
            artifact=d.artifacts.admit(d.scope,canonical(payload),'application/json',trust='operator')
            cases.append({'case_id':'arithmetic-'+str(i),'split':'validation','task_class':'integer_sum','artifact_ref':artifact})
        corpus=CorpusService(d.store,d.artifacts).freeze(self.reviewer,new_id('corpus'),cases,holdout_use_limit=1)
        analysis=self.put('analysis-plan',{'analysis_id':new_id('analysis'),'policy':{'method':'paired_binary_conservative','confidence':0.95,'minimum_tasks':24,'repeats_per_task':1,
               'purpose':'local_qualification','noninferiority_margin':0.25,'safety_failure_limit':0,'missing_policy':'inconclusive'},'scope_note':'Predeclared local control-path drill only; not production acceptance threshold'})
        sampling=self.put('sampling-plan',{'sampling_id':new_id('sampling'),'split':'validation','case_ids':[x['case_id'] for x in cases]})
        holdout=self.put('holdout-policy',{'policy_id':new_id('holdout'),'sealed':True,'max_uses':1})
        budget={'max_wall_seconds':600,'max_attempts':1,'max_tokens':1000,'max_cost_microunits':1000,'currency':'USD','max_parallel_works':1,'max_delegation_depth':0}
        plan={'schema_version':'3.0.0','experiment_id':new_id('experiment'),'scope':d.scope.wire(),'proposal_ref':proposal_ref,'baseline_ref':br,'candidate_ref':cr,
              'corpus_ref':corpus,'verifier_ref':self.prepared['verifier_ref'],'environment_ref':self.prepared['execution_profile']['environment_ref'],'mode':mode,
              'primary_endpoint':'task_all_repeats_pass','analysis_plan_ref':analysis,'sampling_plan_ref':sampling,'safety_stop_rules':['Any safety failure or unknown effect stops the run'],
              'holdout_policy_ref':holdout,'budget':budget,'frozen_at':now()}
        plan['approval_ref']=self.approve('experiment.execute',digest(plan))
        experiment_ref=self.eval.freeze(self.reviewer,plan)
        self.meta.approve_experiment(self.reviewer,proposal['proposal_id'],plan['approval_ref'],experiment_ref)
        return {'proposal_id':proposal['proposal_id'],'proposal_ref':proposal_ref,'experiment_ref':experiment_ref,'baseline_ref':br,'candidate_ref':cr,
                'baseline_release_ref':baseline_release,'candidate_release_ref':candidate_release,'cases':cases}
    def execute_case(self,composition_ref,case,repeat,mode):
        d=self.d;composition=d.store.get(d.scope,'harness-composition',composition_ref)
        from ..runtime.contracts.semantics import resolve_ref
        _,prompt=resolve_ref(d.store,d.scope,composition['prompt_bundle_ref']);payload=json.loads(d.artifacts.read(d.scope,case['artifact_ref']))
        values=payload['numbers'];algorithm=prompt['algorithm']
        if algorithm=='loop_sum':
            result=0
            for value in values:result+=value
        else:result=sum(values)+(1 if algorithm=='wrong_sum' else 0)
        workspace=DataSandbox(self.root/'trials'/new_id('trial'))
        workspace.execute([{'op':'write_json','path':'result.json','content':{'numbers':values,'sum':result}}])
        observed=json.loads(workspace.read('result.json'))
        independent={'success':observed['numbers']==values and observed['sum']==payload['expected'],'safety_failures':0,'unknown_effects':0,
                     'cost_microunits':0,'input_tokens':0,'output_tokens':0,'actual_output_digest':digest(observed),'mode':mode,'composition_ref':composition_ref,
                     'task_id':case['case_id'],'repeat':repeat,'usage_status':'measured',
                     'scope':d.scope.wire(),'risk_class':'low',
                     'target_binding_ref':self.canary_target(case['case_id'])}
        artifact=d.artifacts.admit(d.scope,canonical(independent),'application/json',trust='verifier')
        return TrialObservation(independent['success'],(artifact,),cost_microunits=0,input_tokens=0,output_tokens=0)
    def canary_target(self,task_id):
        return self.d.store.get(self.d.scope,'goal-contract',self.prepared['contract_ref'])['targets'][0]
    def execute(self,prepared, *,rollback=True):
        d=self.d;pid=prepared['proposal_id'];self.meta.start_offline(self.reviewer,pid)
        report_ref=self.eval.run(self.reviewer,prepared['experiment_ref'],self.execute_case)
        report=d.store.get(d.scope,'eval-report',report_ref);self.meta.evaluate(self.reviewer,pid,report_ref)
        if report['verdict']!='pass':return {'state':'offline_evaluated','verdict':report['verdict'],'report_ref':report_ref,'promoted':False}
        task_bindings={c['case_id']:self.canary_target(c['case_id']) for c in prepared['cases'][:2]}
        targets=list({digest(r):r for r in task_bindings.values()}.values())
        policy={'policy_id':new_id('canary-policy'),'eligible_task_ids':[x['case_id'] for x in prepared['cases'][:2]],'max_runs':2,'max_wall_seconds':120,
                'max_cost_microunits':100,'max_trial_cost_microunits':10,'max_trial_tokens':0,'max_concurrent':1,
                'project_opt_in':True,'eligible_risk_classes':['low'],
                'target_binding_refs':targets,'task_binding_refs':task_bindings,
                'abort_on_safety_failure':True,'abort_on_unknown_effect':True,'fallback_release_ref':prepared['baseline_release_ref']}
        policy_ref=self.put('canary-policy',policy)
        approval=self.approve('canary.execute',digest({'proposal_ref':prepared['proposal_ref'],'report_ref':report_ref,'policy_ref':policy_ref}))
        self.meta.approve_canary(self.reviewer,pid,policy_ref,approval);self.meta.start_canary(self.reviewer,pid)
        for case in prepared['cases'][:2]:
            def run_canary(_):
                result=self.execute_case(prepared['candidate_ref'],case,0,'canary')
                return {'success':result.success,'safety_failures':result.safety_failures,'unknown_effects':result.unknown_effects,'cost_microunits':result.cost_microunits,'artifact_ref':result.artifact_refs[0], 'input_tokens':result.input_tokens,'output_tokens':result.output_tokens}
            self.meta.canary_trial(self.reviewer,pid,case['case_id'],run_canary)
        self.meta.request_promotion(self.reviewer,pid)
        plan={'schema_version':'3.0.0','promotion_id':new_id('promotion'),'scope':d.scope.wire(),'expected_active_release_ref':prepared['baseline_release_ref'],
              'candidate_release_ref':prepared['candidate_release_ref'],'eval_report_ref':report_ref,'target_binding_refs':targets,
              'canary_policy_ref':policy_ref,'abort_rules':['No unknown effects','No security regression'],'rollback_release_ref':prepared['baseline_release_ref'],
              'expires_at':(datetime.fromtimestamp(d.store.clock(),timezone.utc)+timedelta(minutes=5)).isoformat().replace('+00:00','Z'),'active_run_policy':'drain'}
        plan['grant_ref']=self.approve('release.promote',digest(plan))
        promoted=self.meta.promote(self.reviewer,pid,plan)
        restored=None
        if rollback:
            approval=self.approve('release.rollback',digest({'target_ref':prepared['baseline_release_ref'],'expected_active_ref':prepared['candidate_release_ref']}))
            restored=self.meta.rollback(self.reviewer,pid,prepared['baseline_release_ref'],prepared['candidate_release_ref'],approval)
        result={'verdict':report['verdict'],'report_ref':report_ref,'promotion':promoted,'rollback':restored,'distinct_tasks':24,
                'scope':'local deterministic arithmetic and lifecycle qualification, not LLM performance or production release'}
        (self.root/'meta-reference-report.json').write_bytes(canonical(result));return result
    def close(self):self.d.close()

def run_meta_reference(root):
    demo=MetaReference(root)
    try:return demo.execute(demo.prepare())
    finally:demo.close()

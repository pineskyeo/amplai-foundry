"""Qualification is a measured report, not a model capability advertisement."""
from __future__ import annotations
from dataclasses import dataclass
from ..runtime.contracts.identity import new_id,now,digest
from ..runtime.errors import Hold,RuntimeFault

MANDATORY=('exact_version','exact_session','ordered_events','cancel_tree','filesystem_containment','egress_containment','secret_isolation','budget_accounting','crash_recovery')
class QualificationRunner:
    def __init__(self,store,artifacts):self.store,self.artifacts=store,artifacts
    def run(self,actor,driver_id,driver_version,environment_ref,probes):
        actor.require('driver.qualify')
        if set(probes)!=set(MANDATORY):raise Hold('QUALIFICATION_COVERAGE','All mandatory real qualification probes must be supplied')
        results=[]
        for name in MANDATORY:
            self.store.assert_outside_tx()
            try: result=probes[name]()
            except Exception as exc:result={'outcome':'inconclusive','reason':type(exc).__name__,'artifact_refs':[]}
            if result.get('outcome') not in {'pass','fail','inconclusive'} or not result.get('artifact_refs'):
                result={'outcome':'inconclusive','reason':'No actual probe evidence','artifact_refs':[]}
            for artifact in result['artifact_refs']:self.artifacts.read(actor.scope,artifact,trusted=True)
            results.append({'name':name,**result})
        report={'qualification_id':new_id('qualification'),'scope':actor.scope.wire(),'driver_id':driver_id,'driver_version':driver_version,
                'environment_ref':environment_ref,'status':'pass' if all(x['outcome']=='pass' for x in results) else 'fail',
                'checks':results,'checked_at':now(),'native_delegation_qualified':False}
        with self.store.tx() as db:return self.store.put(db,actor.scope,'qualification',report['qualification_id'],1,report)

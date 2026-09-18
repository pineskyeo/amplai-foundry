"""Real DEV-02 worker/registry/workspace/session reference without external accounts."""
from __future__ import annotations
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .worker import WorkCoordinator
from ..contracts.identity import canonical
from ..reference import ReferenceDeployment
from ...agent_drivers.ports import DriverRegistry,RecipePort
from ...agent_drivers.protocol import SessionJournal
from ...sandbox.workspace import WorkspaceManager
from ...verification.runtime.service import VerificationObservation


def run_execution_reference(root: Path) -> dict:
    with ReferenceDeployment(root) as d:
        p=d.prepare(two_apps=True)
        registry=DriverRegistry(d.store)
        port=RecipePort(SessionJournal(root/'driver-sessions'))
        registry.register(d.actor,p['execution_profile']['driver_profile_ref'],port)
        workspaces=WorkspaceManager(root/'isolated-workspaces',d.artifacts)
        coordinator=WorkCoordinator(d.runtime,registry,workspaces)
        claims=[d.runtime.claim(d.worker,goal_id=p['goal_id']) for _ in range(2)]
        base=workspaces.empty_snapshot(d.scope)
        def execute(dispatch):
            recipe=p['recipes'][dispatch['node']['work_id']]
            return coordinator.execute(d.worker,dispatch,prompt=json.dumps({'operations':recipe['operations']}),
                base_snapshot=base,output_paths={name:v['path'] for name,v in recipe['outputs'].items()})
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(execute,claims))
        runs=[]
        for dispatch in claims:
            work=d.store.head(d.scope,'work',dispatch['node']['work_id'])
            for acceptance in dispatch['node']['acceptance_ids']:
                output='result-'+dispatch['node']['target_ref']['id']
                d.verification.verify(d.verifier,dispatch['run_id'],acceptance,work['data']['outputs'][output])
            runs.append({'run_id':dispatch['run_id'],**d.verification.finish_work(d.verifier,dispatch['run_id'])})
        def integration(contract,graph,outputs):
            values=[json.loads(d.artifacts.read(d.scope,artifact)) for ports in outputs.values() for artifact in ports.values()]
            good={v['app'] for v in values}=={'alpha','beta'} and all(v['message']=='AMPLAI V3' for v in values)
            return VerificationObservation('pass' if good else 'fail','Independent actual-file integration',{'app_count':len(values)})
        graph=d.store.get(d.scope,'workgraph',p['graph_ref'])
        d.verification.register_global(graph['global_verification_ref'],integration)
        final=d.verification.finish_goal(d.verifier,p['goal_id'],integration)
        report={'stage':'DEV-02','status':d.store.head(d.scope,'goal',p['goal_id'])['state'],
                'goal_id':p['goal_id'],'verification_ref':final,'runs':runs,
                'worker_handoffs':results,'outputs':[str(path.relative_to(root)) for path in sorted(workspaces.root.rglob('result.json'))],
                'budget':d.runtime.budgets.totals(d.scope,p['goal_id']),
                'execution':'real deterministic data-only operations; not a simulated LLM',
                'external_provider_qualified':False,'arbitrary_code_containment_qualified':False}
        (root/'execution-report.json').write_bytes(canonical(report))
        return report

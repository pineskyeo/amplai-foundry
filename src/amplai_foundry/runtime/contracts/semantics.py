"""Cross-object invariants not expressible by JSON Schema alone."""
from __future__ import annotations
from datetime import datetime
from .identity import digest
from ..errors import Hold, RuntimeFault
from ..storage.store import Scope, Store

READINESS_AREAS = frozenset({'terminology','current_behavior','boundary','invariants','ssot','contradictions','acceptance','verifier'})

def walk_refs(value):
    if isinstance(value,dict):
        if set(value)=={'id','revision','digest'}: yield value
        else:
            for child in value.values(): yield from walk_refs(child)
    elif isinstance(value,list):
        for child in value: yield from walk_refs(child)

def resolve_ref(store: Store, scope: Scope, ref: dict, *, db=None) -> tuple[str,dict]:
    import json
    with store._lock:
        rows=(db or store.conn).execute('SELECT kind,digest,data FROM objects WHERE tenant=? AND project=? AND id=? AND revision=?',
                                       (*scope.keys(),ref['id'],ref['revision'])).fetchall()
    matches=[r for r in rows if r['digest']==ref['digest']]
    if len(matches)!=1: raise Hold('REFERENCE_UNRESOLVED','Reference is missing, ambiguous, stale, or outside this project')
    value=json.loads(matches[0]['data'])
    if digest(value)!=ref['digest']: raise Hold('REFERENCE_CORRUPT','Stored reference content does not match its immutable digest')
    return matches[0]['kind'],value

def check_refs(store: Store, scope: Scope, value: dict, *, db=None) -> None:
    for ref in walk_refs(value): resolve_ref(store,scope,ref,db=db)

def check_readiness(entries: list[dict], *, na_rules: set[str] | None=None) -> None:
    areas=[item['area'] for item in entries]
    if len(areas)!=8 or set(areas)!=READINESS_AREAS:
        raise Hold('READINESS_INCOMPLETE','All eight distinct readiness areas are required')
    for item in entries:
        if item['status']=='ready' and (not item['source_refs'] or not item['reason']):
            raise Hold('READINESS_UNGROUNDED','Ready requires current evidence and a reason')
        if item['status']=='not_applicable':
            if not any(item['reason'].startswith(rule+':') for rule in (na_rules or set())):
                raise Hold('READINESS_NA_RULE','N/A requires an approved applicability rule')
        elif item['status']!='ready':
            raise Hold('KNOWLEDGE_NOT_READY','Unresolved knowledge: '+item['area'])

def check_context(bundle: dict, governing_refs: list[dict]) -> None:
    if not bundle['governing_set_complete']:
        raise Hold('CONTEXT_INCOMPLETE','Governing context must not be truncated')
    actual={digest(ref) for ref in bundle['core_refs']}
    if not {digest(ref) for ref in governing_refs}<=actual:
        raise Hold('CONTEXT_GOVERNING_MISSING','A required rule or decision was omitted')
    for entry in bundle['entries']:
        if entry['mandatory'] and (entry['freshness']!='current' or entry['superseded_by'] is not None):
            raise Hold('CONTEXT_STALE','A mandatory context entry has been superseded or is stale')

def check_contract(contract: dict, *, previous: dict | None=None) -> None:
    if not contract['objective'].strip():
        raise Hold('OBJECTIVE_EMPTY', 'A whitespace-only objective is not actionable')
    if any(not c['statement'].strip() for c in contract['constraints']):
        raise Hold('CONSTRAINT_EMPTY', 'A constraint must state an actual boundary')
    if any(not c['statement'].strip() for c in contract['acceptance']):
        raise Hold('ACCEPTANCE_EMPTY', 'Acceptance must state an observable result')
    criteria=contract['acceptance']; ids=[a['id'] for a in criteria]
    if len(ids)!=len(set(ids)): raise RuntimeFault('ACCEPTANCE_DUPLICATE','Acceptance identifiers must be unique')
    if not any(c['mandatory'] for c in criteria): raise Hold('NO_MANDATORY_ACCEPTANCE','At least one mandatory observable result is required')
    for criterion in criteria:
        if criterion['mandatory'] and (not criterion['verifier_ref'] or not criterion['required_evidence_types'] or not criterion['success_rule'].strip()):
            raise Hold('ACCEPTANCE_UNBOUND','Mandatory acceptance must bind verifier, evidence and success rule')
    if contract['open_question_refs']:
        raise Hold('OPEN_QUESTION','Resolve material questions before admitting execution')
    if any(a['blocks_execution'] and a['status']!='verified' for a in contract['assumptions']):
        raise Hold('UNVERIFIED_ASSUMPTION','A blocking assumption has not been verified')
    if contract['mode']=='design':
        allowed={'workspace.design_write','workspace.read','knowledge.read'}
        if any(c['action'] not in allowed or c['effect_class'] not in {'pure_read','sandbox_write'} for c in contract['requested_capabilities']):
            raise RuntimeFault('DESIGN_CAPABILITY','Design mode does not permit implementation or deployment')
    if previous:
        if contract['goal_id']!=previous['goal_id'] or contract['revision']!=previous['revision']+1:
            raise RuntimeFault('CONTRACT_REVISION','A changed contract needs the next explicit revision')
        protected={c['id']:c for c in previous['constraints'] if c['protected']}
        proposed={c['id']:c for c in contract['constraints']}
        if any(proposed.get(k)!=v for k,v in protected.items()):
            raise Hold('PROTECTED_CONSTRAINT','A protected constraint cannot be removed by re-planning')

def ordered_times(start: str, finish: str) -> None:
    a=datetime.fromisoformat(start.replace('Z','+00:00')); b=datetime.fromisoformat(finish.replace('Z','+00:00'))
    if not start.endswith('Z') or not finish.endswith('Z') or b<=a:
        raise RuntimeFault('TIME_ORDER','Canonical UTC expiry must be after its start')

"""Execution authority derived from an authenticated, existing governance decision.

Adapters are trusted server configuration, never HTTP request fields. Deployment must
provide a live decision resolver. The local demo supplies an isolated test authority.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from .identity import digest, sign, verify_signature, now, reference
from .registry import Contracts
from ..errors import Conflict, Hold, RuntimeFault
from ..storage.store import Store, Scope

@dataclass(frozen=True)
class Actor:
    subject_id: str
    scope: Scope
    permissions: frozenset[str]
    kind: str='human'
    authn_context_ref: str=''
    def require(self, permission: str):
        if permission not in self.permissions: raise RuntimeFault('FORBIDDEN','Actor lacks required permission: '+permission)
    def wire(self):
        return {'subject_id':self.subject_id,'kind':self.kind,'authn_context_ref':self.authn_context_ref}

def capability_contains(ceiling: list[dict], requested: dict) -> bool:
    # Exact resources, no wildcard strings, shell fragments or prefix escalation.
    return any(all(c.get(k)==requested.get(k) for k in ('action','resource','effect_class')) for c in ceiling)

def intersect_capabilities(requested: list[dict], *ceilings: list[dict], denied: list[dict] | None=None) -> list[dict]:
    denied=denied or []
    return [r for r in requested if all(capability_contains(c,r) for c in ceilings) and not capability_contains(denied,r)]

class Authority:
    def __init__(self, store: Store, contracts: Contracts, trusted_keys: dict[str,Ed25519PublicKey],
                 decision_resolver: Callable[[Scope,dict],dict], *, signer: Ed25519PrivateKey | None=None, key_id: str=''):
        self.store,self.contracts,self.keys=store,contracts,trusted_keys
        self.resolver,self.signer,self.key_id=decision_resolver,signer,key_id
    def _decision(self, scope: Scope, ref: dict) -> dict:
        self.store.assert_outside_tx()
        try: result=self.resolver(scope,ref)
        except RuntimeFault: raise
        except Exception as exc: raise Hold('AUTHORITY_UNAVAILABLE','Live authority could not be checked') from exc
        if result.get('scope')!=scope.wire() or result.get('status')!='approved' or result.get('revoked'):
            raise Hold('AUTHORITY_DENIED','Decision is missing, revoked, or outside the project')
        return result
    def issue(self, actor: Actor, grant: dict) -> dict:
        actor.require('grant.issue')
        if not self.signer: raise Hold('SIGNING_UNAVAILABLE','No configured signing authority')
        if grant.get('scope')!=actor.scope.wire(): raise RuntimeFault('SCOPE_MISMATCH','Grant scope differs')
        decision=self._decision(actor.scope,grant['decision_ref'])
        if decision.get('issuer_subject_id')!=actor.subject_id:
            raise RuntimeFault('ISSUER_MISMATCH','Only the governed decision issuer can authorize this grant')
        if decision.get('generation')!=grant['generation']: raise Hold('STALE_AUTHORITY','Authority generation changed')
        for field in ('contract_ref','graph_ref','subject_id'):
            if decision.get(field)!=grant.get(field): raise RuntimeFault('DECISION_BINDING','Decision does not authorize this '+field)
        if len(intersect_capabilities(grant['capabilities'],decision.get('capabilities',[])))!=len(grant['capabilities']):
            raise RuntimeFault('CAPABILITY_ESCALATION','Grant expands the governed capability ceiling')
        value={**grant,'issuer':actor.wire()}
        value=sign(value,self.key_id,self.signer); self.contracts.validate('execution-grant',value)
        ref=reference(value,'grant_id')
        with self.store.tx() as db:
            self.store.put(db,actor.scope,'execution-grant',value['grant_id'],1,value)
        return ref
    def preflight(self, scope: Scope, ref: dict, *, subject_id: str, contract_ref: dict, graph_ref: dict,
                  capabilities: list[dict], effect_key: str | None=None, artifact_digest: str | None=None) -> dict:
        grant=self.store.get(scope,'execution-grant',ref)
        self.contracts.validate('execution-grant',grant); verify_signature(grant,self.keys)
        decision=self._decision(scope,grant['decision_ref'])
        timestamp=datetime.fromtimestamp(self.store.clock(),tz=__import__('datetime').timezone.utc)
        if not datetime.fromisoformat(grant['not_before'].replace('Z','+00:00'))<=timestamp<datetime.fromisoformat(grant['expires_at'].replace('Z','+00:00')):
            raise Hold('GRANT_EXPIRED','Execution grant is not currently valid')
        for key,value in [('subject_id',subject_id),('contract_ref',contract_ref),('graph_ref',graph_ref)]:
            if grant[key]!=value: raise Hold('GRANT_BINDING','Grant does not match the active execution')
        if grant['generation']!=decision.get('generation'): raise Hold('GRANT_REVOKED','Policy generation has changed')
        if grant['effect_key'] is not None and effect_key!=grant['effect_key']:
            raise Hold('EFFECT_BINDING','Grant is bound to another effect')
        if artifact_digest is not None and grant['artifact_bounds'] and artifact_digest not in grant['artifact_bounds']:
            raise Hold('PAYLOAD_BINDING','Effect payload is outside approved artifact bounds')
        if len(intersect_capabilities(capabilities,grant['capabilities'],decision.get('capabilities',[])))!=len(capabilities):
            raise Hold('CAPABILITY_DENIED','Current authority does not cover requested capabilities')
        return grant
    def consume(self, db, scope: Scope, grant: dict, effect_key: str, payload_digest: str) -> None:
        row=db.execute('SELECT payload_digest FROM grant_uses WHERE tenant=? AND project=? AND grant_id=? AND effect_key=?',
                       (*scope.keys(),grant['grant_id'],effect_key)).fetchone()
        if row:
            if row[0]!=payload_digest: raise Conflict('GRANT_REUSE_CONFLICT','Effect key is bound to another payload')
            return
        used=db.execute('SELECT COUNT(*) FROM grant_uses WHERE tenant=? AND project=? AND grant_id=?',(*scope.keys(),grant['grant_id'])).fetchone()[0]
        if used>=grant['max_uses']: raise Hold('GRANT_EXHAUSTED','Execution grant use budget exhausted')
        db.execute('INSERT INTO grant_uses VALUES(?,?,?,?,?,?,?)',(*scope.keys(),grant['grant_id'],grant['generation'],effect_key,payload_digest,now()))

"""Frozen corpora with split ACLs, exposure budgets, provenance and contamination.

The CP's private store is a service trust boundary, not physical secrecy from its
operator. A corpus stored in the same local project is explicitly ``local_acl``;
claiming an independent hidden holdout requires an external evaluator boundary.
"""
from __future__ import annotations

from copy import deepcopy

from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.identity import digest, now
from amplai_foundry.runtime.errors import Hold, RuntimeFault

SPLITS = frozenset({'development', 'validation', 'holdout'})


class CorpusService:
    def __init__(self, store, artifacts):
        self.store, self.artifacts = store, artifacts

    def freeze(self, actor: Actor, corpus_id: str, cases: list[dict], *,
               holdout_use_limit: int, metadata: dict | None = None) -> dict:
        actor.require('corpus.manage')
        if 'harness.propose' in actor.permissions:
            raise Hold('CORPUS_PROPOSER', 'Proposers may propose cases but not publish protected corpus versions')
        if not isinstance(cases, list) or not cases or len(cases) > 100000:
            raise RuntimeFault('CORPUS_IDS', 'Use a nonempty bounded list of cases')
        if (not isinstance(corpus_id, str) or not corpus_id.strip() or
                any(not isinstance(c, dict) or not isinstance(c.get('case_id'), str)
                    or not c['case_id'].strip() for c in cases)):
            raise RuntimeFault('CORPUS_IDS', 'Corpus and case identifiers must be nonempty strings')
        if len({c['case_id'] for c in cases}) != len(cases):
            raise RuntimeFault('CORPUS_IDS', 'Case IDs must be unique')
        if type(holdout_use_limit) is not int or not 1 <= holdout_use_limit <= 1000:
            raise RuntimeFault('HOLDOUT_LIMIT', 'An explicit bounded positive reuse limit is required')
        if any(not isinstance(c.get('artifact_ref'), dict) or not isinstance(c['artifact_ref'].get('digest'), str) for c in cases):
            raise RuntimeFault('CORPUS_ARTIFACT', 'Every case must name an immutable artifact')
        if len({c['artifact_ref']['digest'] for c in cases}) != len(cases):
            raise Hold('CORPUS_DUPLICATE_TASK', 'Identical input bytes cannot count as independent tasks or cross splits')
        meta = deepcopy(metadata or {})
        allowed = {'license', 'privacy_class', 'provenance', 'secrecy_boundary', 'verifier_ref', 'environment_ref'}
        if set(meta) - allowed:
            raise RuntimeFault('CORPUS_METADATA', 'Unknown corpus metadata field')
        if meta.get('secrecy_boundary', 'local_acl') != 'local_acl':
            raise Hold('HOLDOUT_SECRECY', 'This service cannot attest a remote physical holdout boundary')
        meta.setdefault('license', 'unspecified-no-redistribution')
        meta.setdefault('privacy_class', 'internal')
        meta.setdefault('provenance', 'operator-supplied; not independently attested')
        meta['secrecy_boundary'] = 'local_acl'
        if meta['privacy_class'] not in {'public', 'internal', 'confidential', 'restricted'}:
            raise RuntimeFault('CORPUS_METADATA', 'Unknown privacy class')
        for case in cases:
            if case.get('split') not in SPLITS:
                raise RuntimeFault('CORPUS_SPLIT', 'Unknown corpus split')
            if not isinstance(case.get('task_class'), str) or not case['task_class'].strip():
                raise RuntimeFault('TASK_CLASS', 'Each case needs an explicit task class')
            self.artifacts.read(actor.scope, case['artifact_ref'])
        # Matching an old, exposed task under a new corpus ID must not reset its history.
        with self.store._lock:
            rows = self.store.conn.execute("SELECT data FROM objects WHERE tenant=? AND project=? AND kind='eval-corpus'",
                                           actor.scope.keys()).fetchall()
        import json
        old_cases = [c for row in rows for c in json.loads(row['data'])['cases']]
        exposed = {c['artifact_ref']['digest'] for c in old_cases if c['split'] != 'holdout'}
        findings = [{'case_id': c['case_id'], 'reason': 'previously_non_holdout'}
                    for c in cases if c['split'] == 'holdout' and c['artifact_ref']['digest'] in exposed]
        value = {'corpus_id': corpus_id, 'scope': actor.scope.wire(), 'cases': deepcopy(cases),
                 'holdout_use_limit': holdout_use_limit, 'frozen_at': now(), 'metadata': meta,
                 'contamination_findings': findings}
        with self.store.tx() as db:
            return self.store.put(db, actor.scope, 'eval-corpus', corpus_id, 1, value)

    def select(self, actor: Actor, corpus_ref: dict, split: str, *, purpose: str) -> list[dict]:
        actor.require('corpus.read')
        if split not in SPLITS:
            raise RuntimeFault('CORPUS_SPLIT', 'Unknown corpus split')
        if 'harness.propose' in actor.permissions and split != 'development':
            raise Hold('HOLDOUT_PROPOSER', 'Proposers may inspect development cases only')
        corpus = self.store.get(actor.scope, 'eval-corpus', corpus_ref)
        if split == 'holdout':
            actor.require('corpus.holdout.evaluate')
            if purpose != 'frozen_experiment':
                raise Hold('HOLDOUT_PURPOSE', 'Holdout is read only for a frozen approved experiment')
        return deepcopy([c for c in corpus['cases'] if c['split'] == split])

    def consume_holdout(self, actor: Actor, corpus_ref: dict, experiment_ref: dict) -> None:
        actor.require('corpus.holdout.evaluate')
        if 'harness.propose' in actor.permissions:
            raise Hold('HOLDOUT_PROPOSER', 'Proposer cannot consume holdout')
        corpus = self.store.get(actor.scope, 'eval-corpus', corpus_ref)
        experiment = self.store.get(actor.scope, 'eval-experiment', experiment_ref)
        if experiment['corpus_ref'] != corpus_ref:
            raise Hold('HOLDOUT_BINDING', 'Experiment does not bind this exact corpus')
        if corpus.get('contamination_findings'):
            raise Hold('HOLDOUT_CONTAMINATED', 'This corpus already contains exposed holdout tasks')
        # Artifact digest keys span corpus versions; a copied corpus does not reset exposure.
        keys = [corpus_ref['id']] + ['case-' + c['artifact_ref']['digest'][7:]
                                    for c in corpus['cases'] if c['split'] == 'holdout']
        with self.store.tx() as db:
            updates = []
            for key in keys:
                try:
                    head = self.store.head(actor.scope, 'holdout-use', key, db=db)
                except RuntimeFault as exc:
                    if exc.code != 'NOT_FOUND':
                        raise
                    head = {'row_version': 0, 'data': {'experiment_refs': [], 'contaminated': False,
                                                     'use_limit': corpus['holdout_use_limit']}}
                data = head['data']
                refs = data['experiment_refs']
                limit = min(data.get('use_limit', corpus['holdout_use_limit']), corpus['holdout_use_limit'])
                if data['contaminated'] or (experiment_ref not in refs and len(refs) >= limit):
                    raise Hold('HOLDOUT_EXHAUSTED', 'Holdout is contaminated or its cross-version exposure budget is exhausted')
                if experiment_ref not in refs:
                    updates.append((key, head, {**data, 'experiment_refs': [*refs, experiment_ref], 'use_limit': limit}))
            for key, head, data in updates:
                self.store.cas(db, actor.scope, 'holdout-use', key, head['row_version'], 'active', data)
            self.store.event(db, actor.scope, 'release', corpus_ref['id'], 'holdout.consumed',
                             {'experiment_ref': experiment_ref, 'case_count': len(keys) - 1})

    def contamination(self, scope, corpus_ref: dict) -> bool:
        corpus = self.store.get(scope, 'eval-corpus', corpus_ref)
        if corpus.get('contamination_findings'):
            return True
        keys = [corpus_ref['id']] + ['case-' + c['artifact_ref']['digest'][7:]
                                    for c in corpus['cases'] if c['split'] == 'holdout']
        for key in keys:
            try:
                if self.store.head(scope, 'holdout-use', key)['data'].get('contaminated'):
                    return True
            except RuntimeFault as exc:
                if exc.code != 'NOT_FOUND':
                    raise
        return False

    def mark_contaminated(self, actor: Actor, corpus_ref: dict, reason: str):
        actor.require('corpus.manage')
        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeFault('CONTAMINATION_REASON', 'An audit reason is required')
        corpus = self.store.get(actor.scope, 'eval-corpus', corpus_ref)
        keys = [corpus_ref['id']] + ['case-' + c['artifact_ref']['digest'][7:]
                                    for c in corpus['cases'] if c['split'] == 'holdout']
        with self.store.tx() as db:
            for key in keys:
                try:
                    head = self.store.head(actor.scope, 'holdout-use', key, db=db)
                except RuntimeFault as exc:
                    if exc.code != 'NOT_FOUND':
                        raise
                    head = {'row_version': 0, 'data': {'experiment_refs': []}}
                self.store.cas(db, actor.scope, 'holdout-use', key, head['row_version'], 'contaminated',
                               {**head['data'], 'contaminated': True, 'reason': reason})
            self.store.event(db, actor.scope, 'release', corpus_ref['id'], 'holdout.contaminated', {'reason': reason})

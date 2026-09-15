// Read-only traceability validation; this does not execute or approve acceptance cases.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const feature = dirname(fileURLToPath(import.meta.url));
const originalRoot = process.argv[2];
assert(originalRoot, 'Pass the original Synapse source checkout, read-only.');
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const read = path => JSON.parse(readFileSync(path, 'utf8'));
const mapPath = resolve(feature, 'acceptance-map.json');
const map = read(mapPath);
const expected = {
  'requirements.json': '5063beb80c88266378c844f1c091d16fae7cea9438f496e6a8caf34cac4bd224',
  'acceptance.json': 'ee49350982eeaefa11e0369cef3fee152f4e1c647f5557be5e32045d8a1cdce1',
};
const originals = {};
for (const [name, digest] of Object.entries(expected)) {
  const bytes = readFileSync(resolve(originalRoot, 'specs/024-synapse-app-split/reference', name));
  assert.equal(sha(bytes), digest, 'Immutable original source changed: ' + name);
  originals[name] = JSON.parse(bytes);
}
const ids = rows => rows.map(row => row.id).sort();
const reqIds = ids(map.requirements);
assert.equal(new Set(reqIds).size, 23);
assert.equal(map.requirements.length, 23);
const originalRequirements = originals['requirements.json'].requirements.filter(row => reqIds.includes(row.id));
assert.deepEqual(ids(originalRequirements), reqIds);
const requiredCaseIds = originalRequirements.flatMap(row => row.acceptance_ids).sort();
assert.equal(requiredCaseIds.length, 46);
assert.deepEqual(ids(map.acceptance), requiredCaseIds);
for (const row of map.acceptance) {
  const original = originals['acceptance.json'].cases.find(item => item.id === row.id);
  assert(original, 'Missing original case');
  assert.equal(row.kind, original.kind);
  assert.equal(row.declared_environment, original.environment);
  assert.deepEqual(row.requirement_ids, original.requirement_ids);
  assert.equal(row.source_selector.id, original.id);
  for (const evidence of row.execution_evidence) {
    assert(existsSync(resolve(feature, evidence.path)), 'Missing local evidence artifact');
  }
}
const executed = map.acceptance.filter(row => row.execution_evidence.length > 0).length;
assert.equal(map.summary.executed_cases, executed);
assert.equal(map.summary.original_cases, 46);
assert.equal(map.summary.requirements, 23);
assert.equal(map.planned_slices.length, 8);
const sourceHashes = Object.fromEntries(['spec.md', 'plan.md', 'tasks.md', 'work-contract.json',
  'acceptance-map.json'].map(path => [path, sha(readFileSync(resolve(feature, path)))]));
console.log(JSON.stringify({
  status: 'PASS_TRACEABILITY_ONLY', requirements: 23, cases: 46, planned_slices: 8,
  executed_cases: executed, passed_cases: map.summary.passed_cases, missing_cases: [],
  source_hashes: map.sources, current_source_hashes: sourceHashes,
  method: 'Read-only original SHA256, exact requirement/case IDs, kind, declared environment, source selectors and retained evidence presence; not a behavior-test or independent-review verdict.',
  limits: 'S08 integration and independent reviews are separate. No native full Work, Python3.6, RHEL7 or deployment PASS.',
}, null, 2));

// Read-only local reference verification. No build, activation, install or new release.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

const [source, apps] = process.argv.slice(2);
assert(source && apps, 'Pass the Synapse source and independent app roots, read-only.');
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const read = path => readFileSync(resolve(source, path));
const scopePath = 'specs/025-synapse-independent-apps/review-scope.json';
const scope = JSON.parse(read(scopePath));
const scopeCore = { ...scope };
delete scopeCore.scope_sha256;
assert.equal(sha(JSON.stringify(scopeCore)), '6ceee816cb838751749ddafdb8e396f6af047bc811e6a7edbd3d80d59d2fb312');
function checkFrozen() {
  for (const row of [...scope.files, ...scope.governing_artifacts]) {
    if (row.path === 'specs/025-synapse-independent-apps/evidence-trace.jsonl') {
      const bytes = read(row.path);
      const lines = bytes.toString('utf8').split(/(?<=\n)/);
      let prefix = '';
      let matched = false;
      for (const line of lines) {
        prefix += line;
        if (sha(prefix) === row.sha256) { matched = true; break; }
      }
      assert(matched, 'Reviewed trace prefix was rewritten, not merely appended');
      const suffix = bytes.toString('utf8').slice(prefix.length).trim().split('\n').filter(Boolean).map(JSON.parse);
      assert.deepEqual(suffix.map(row => row.event), ['review_pass', 'gardening_completed', 'done']);
      continue;
    }
    assert.equal(sha(read(row.path)), row.sha256, 'Frozen W002 input drift: ' + row.path);
  }
}
checkFrozen();
const { loadRelease } = await import(pathToFileURL(resolve(source, 'tools/app-split/compose-ui.mjs')));
const { verifyFoundation } = await import(pathToFileURL(resolve(source, 'tools/app-split/foundation.mjs')));
const release = loadRelease(resolve(apps, 'release-2026-09-11-r7'));
assert.equal(release.manifest.release_sha256, '51deabe0b4dad6881837f8cfbe414da22ba6a2e23bb86f32b206802b254c01bd');
const foundation = verifyFoundation(source, resolve(apps, 'synapse-core'), resolve(apps, 'synapse-contracts'));
assert.equal(foundation.status, 'PASS');
const ontology = resolve(source, 'third_party/dc-ontology');
function git(...args) {
  const result = spawnSync('git', ['-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
    '--no-optional-locks', ...args], { cwd: ontology, encoding: 'utf8', timeout: 15000 });
  assert.equal(result.status, 0, 'Ontology read-only Git check failed');
  return result.stdout.trim();
}
const ontologyRevision = git('rev-parse', 'HEAD');
assert.equal(ontologyRevision, '7863f885de43258b399f62d9e0a02f068b703308');
assert.equal(git('status', '--porcelain=v1', '-uno'), '');
const ontologyManifest = sha(read('third_party/dc-ontology/manifest.json'));
assert.equal(ontologyManifest, '81364858c77ec9fd9b0f8f6ae0d8cc26432f116c136481f83b782af2fdd651df');
checkFrozen();
assert.equal(loadRelease(resolve(apps, 'release-2026-09-11-r7')).snapshot, release.snapshot);
const lock = JSON.parse(read('tools/app-split/foundation-lock.json'));
console.log(JSON.stringify({
  schema_version: '1.0', verdict: 'PASS',
  scope: 'Immutable W002 local reference pins for an INTERNAL Kit guide; not a deployment BOM or runtime-compatibility approval.',
  verified: { frozen_source_files: scope.files.length, governing_artifacts: scope.governing_artifacts.length,
    ui_payload_files: release.manifest.files.length, foundation },
  components: {
    ui: { revision: release.manifest.release_sha256, manifest_sha256: sha(readFileSync(resolve(apps, 'release-2026-09-11-r7/release.json'))), activation: 'NOT_ACTIVATED' },
    backend: { revision: 'catalog-' + lock.catalog_sha256, catalog_sha256: lock.catalog_sha256,
      w002_scope_sha256: scope.scope_sha256, core_library_sha256: foundation.core_sha256 },
    contracts: { revision: foundation.contracts_sha256, catalog_sha256: lock.catalog_sha256 },
    ontology: { revision: ontologyRevision, manifest_sha256: ontologyManifest, version: '0.3.0',
      scope: 'Pinned tracked source and manifest only; no domain mutation or new ontology release.' },
  },
  limits: ['No real server, RHEL7, RPM install, production API or backend restart.',
    'W004 owns a deployable release BOM and target verification; this observation only freezes guide reference inputs.'],
}, null, 2));

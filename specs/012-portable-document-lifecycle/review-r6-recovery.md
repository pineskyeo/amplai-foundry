# W003 R6 Failure / Recovery Lens

Independent sequential lens. No other R6 lens conclusions were supplied or used.

P0: 0. P1: 0. Blocking-P2: 0. Advisory: 0.

No concrete failure/recovery finding remained in the inspected scope and executed checks.
This is a local failure/recovery review verdict, not whole-goal completion, release,
deployment, or approval of unavailable target environments.

## Frozen Scope And Integrity

Owning root: `/Users/pinesky/workspace/amplai-foundry`.
Base and observed HEAD before/after: `6e005a5e3035ef05510c403a7cd691d524b733bd`.
Scope: `61462cceca36c9f4554835729e0ea4a180db6f40e83e8c937358eb16c26a297d`.
Envelope SHA256: `31d5050daedbaed4f382f694341ec0d139a39bc575cf11e705e7305734b28a94`.
Archive: `/tmp/synapse-split-resume.t7Jusk/w003-review-scope-r6-source-snapshot.tar.gz`.
Archive SHA256: `57c486bd01777fd188d19d46409094dede8b3e63599d543f7af3a08d6c918a6e`; mode 0600.

Executed Node checks before and after: parse envelope, remove only `scope_sha256`,
hash UTF-8 `JSON.stringify` in preserved property order; hash/stat each of 155 source
and 621 governing files; run `git rev-parse HEAD`. Both checks returned zero mismatches.
The after check also checked all 776 files in the disposable test copy: zero mismatches.
Executed Python tarfile check: exactly 777 unique regular members, exact membership,
every member's bytes, size and mode equal to the envelope (including envelope itself).
Archive digest and mode were checked again after testing and remained unchanged.

Captured integrity output:

```text
{"root":"/Users/pinesky/workspace/amplai-foundry","files":776,"errors":[]}
{"root":"/tmp/w003-r6-recovery.NA7ayW/tree","files":776,"errors":[]}
ARCHIVE: 777 exact unique regular members; every member size/mode/SHA256 matches envelope and shared bytes
```

The excluded doc-impact.json is a derived projection under the explicit envelope policy.
No underlying freshness waiver was applied. Parent supplied its owning-root validation
after projection regeneration: exit 0, RESOLVED, errors [], snapshot
`b44fc0bb30a512d1bed7d3e36db2c33d846af96541908809b99d8588ae29d926`, two views MATCH.
This lens did not independently execute that owning-root command.

## Executed Checks

The disposable tree was created with `git archive 6e005a5e3035ef05510c403a7cd691d524b733bd`
and overlaid with the exact frozen archive. No live ignored runtime, Store, Vault,
private host settings or other repository state was copied into test fixtures.
No Git worktree or shared Git-state mutation was performed. No source mutation was
introduced. Synthetic fault monkeypatches were context-managed and restored.

The following commands ran in `/tmp/w003-r6-recovery.NA7ayW/tree`:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=/tmp/w003-r6-recovery.NA7ayW/tree/src:/tmp/w003-r6-recovery.NA7ayW/tree/tests /Users/pinesky/workspace/amplai-foundry/.venv/bin/python -B -m pytest -p no:cacheprovider -q tests/ai/test_document_optional_aliases.py tests/ai/test_document_reference_resources.py tests/ai/test_document_review.py tests/ai/test_document_preservation.py tests/ai/test_portable_baseline.py tests/ai/test_document_html.py tests/ai/test_document_typed_visibility.py

PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=/tmp/w003-r6-recovery.NA7ayW/tree/src:/tmp/w003-r6-recovery.NA7ayW/tree/tests /Users/pinesky/workspace/amplai-foundry/.venv/bin/python -B -m pytest -p no:cacheprovider -q tests/ai/test_amplai_kit_installer.py tests/ai/test_amplai_kit_regressions.py tests/ai/test_amplai_async_runtime.py

PYTHONDONTWRITEBYTECODE=1 /Users/pinesky/workspace/amplai-foundry/.venv/bin/python -B /tmp/w003-r6-recovery.NA7ayW/probe.py
python3 -B scripts/loopctl.py trace verify specs/012-portable-document-lifecycle/evidence-trace.jsonl
python3 -B tools/amplai-loop-kit/seal.py --verify
```

First pytest command: exit 0, 520 collected tests, all passed; no skip/failure symbols.
Second pytest command: exit 0, 101 collected tests, all passed; no skip/failure symbols.
The repository's extra quiet setting suppressed the usual summary. Each corresponding
`--collect-only -q` command was also executed to establish counts independently:

```text
tests/ai/test_document_html.py: 50
tests/ai/test_document_optional_aliases.py: 88
tests/ai/test_document_preservation.py: 39
tests/ai/test_document_reference_resources.py: 105
tests/ai/test_document_review.py: 65
tests/ai/test_document_typed_visibility.py: 102
tests/ai/test_portable_baseline.py: 71

tests/ai/test_amplai_async_runtime.py: 28
tests/ai/test_amplai_kit_installer.py: 22
tests/ai/test_amplai_kit_regressions.py: 51
```

Final raw progress from the first run:

```text
....................... [ 83%]
........................................................................ [ 96%]
................                                                         [100%]
```

Final raw progress from the second run:

```text
................................................................... [ 71%]
.............................                                            [100%]
```

Actual imports were inspected before trusting results. Engine, application and fixture
helper `__file__` values all resolved under the disposable tree:

```text
/tmp/w003-r6-recovery.NA7ayW/tree/scripts/amplai_docs.py
/tmp/w003-r6-recovery.NA7ayW/tree/src/amplai_foundry/__init__.py
/tmp/w003-r6-recovery.NA7ayW/tree/tests/ai/test_document_lifecycle.py
/tmp/w003-r6-recovery.NA7ayW/tree/tests/ai/test_document_html.py
```

Independent probe.py executed the exact source engine and exact canonical payload engine.
Each emitted status PASS for: duplicate/nonfinite/deep JSON denial; FIFO nonblocking
denial and exact file-byte limit; concurrent writer rejection and lock release;
simulated ENOSPC publication failure preserving old bytes and mode0600, removing the
uncommitted stage and allowing a successful retry. Probe exit 0. No external content
or real Store/native host operation was involved.

Trace command output, exit 0:

```json
{"errors": [], "events": 144, "path": "specs/012-portable-document-lifecycle/evidence-trace.jsonl", "valid": true}
```

Seal command output, exit 0:

```json
{"baseline_stale": [], "checksum_mismatched": [], "checksum_missing_file": [], "hint": null, "manifest_stale": [], "not_in_checksums": [], "ok": true}
```

## Inspected Mechanisms And Evidence

Read the governing work contract, specification, plan/refinements, task acceptance and
implementation fields, acceptance map, readiness/discovery, Context, environment,
trace and R6 pre-review handoff, applicable rules and permissions. Inspected actual
dirty diffs, engine code and fixture assertions. Main implementation surfaces:

- `scripts/amplai_docs.py:961`: bounded no-follow SourceTree reads and drift checks.
- `scripts/amplai_docs.py:1764` and `:3704`: pre-output classification, unsafe aliases,
  optional absence, logical/resolved identity and bounded candidate prefixes.
- `scripts/amplai_docs.py:2148` and `:2224`: exact serialized budget, owner/group/mode,
  identity/content rechecks, atomic publication and nonblocking review serialization.
- `scripts/amplai_docs.py:521`, `:620`, `:4733`: exact authorized fixture retirement,
  validated recovery, exclusive/idempotent bundles and preservation of foreign edits.
- `tools/amplai-loop-kit/install.py:1320` and `:1387`: journaled transactions, preflight
  drift, backups, interrupted recovery and borrowed-state preservation.
- `scripts/amplai.py:258`, `scripts/amplai_runtime.py:2556`: allowlisted lease display
  and terminal read-only Work projection; authority remains in the existing Store.

Executed suites include actual killed-process recovery; competing writer rejection;
late content/mode/identity changes; accumulated-history/projection byte overflow;
corrupt backup/index/journal rejection; safe retry; partial bundle cleanup; unsafe
leaf/parent/alternative aliases and special-file/error/budget cases; source/payload/
installed consumers; local-edit/permission preservation; authoritative synthetic Store
dependency/lease and terminal projection behavior. These are fixture proofs.

Inspected the complete retained `s08-canonical-eval-r6.log`: EVAL PASS, 2494 passed,
4 existing live Slack deselections, 11 V2 checks, seal, 12 selftests and actual guide
Context delivery. Inspected S19/S20 and all fourteen predecessor revalidation records,
current source-review evidence r13, actual Context and guide-delivery evidence, R6
view/native evidence. These retained evaluator claims were not represented as a newly
executed full canonical run by this reviewer. Counts overlap and are not summed with
the independent checks.

The twelve referenced current browser artifacts under `/tmp/w003-browser-r6.Pr7MvH/`
exist and every recorded size/SHA256 matched. Frozen HTML files also matched the scope.
Their browser results are retained evidence; separate focused HTML tests executed
their own isolated browser fixtures. No native Claude/Codex transport probe was rerun.

## Limits

No current owning-root Context/docs validation or full canonical S08 rerun was performed
by this lens. The disposable base/snapshot lacks optional ignored runtime references;
it was not used to certify or invalidate actual owning-root freshness. The retained
actual Context hash is `a66d9e62b971543b077052dbeef92a9f0533eab7c72808fa3d42689e3ab1ab10`.
No freshness exception or false PASS was inferred from an isolated-copy difference.

Python3.6 runtime, RHEL7, full native Work/resume, signing, fleet activation and actual
deployment remain unverified. W004 packaging remains separate and unimplemented.
Original 23 requirements / 46 cases retain zero final PASS pending completion gates.
No live Slack/network/production/native-host action, publication, real retirement,
Store/Vault mutation or shared source/cache/Git modification was performed.

VERDICT: PASS

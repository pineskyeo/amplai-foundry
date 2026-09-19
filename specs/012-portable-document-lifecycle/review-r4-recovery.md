# W003 R4 Independent Failure/Recovery Review

Work: CR-SYNAPSE-APP-SPLIT-W003
Feature: specs/012-portable-document-lifecycle
Lens: FAILURE/RECOVERY, independently assessed without another R4 reviewer's conclusions.
Review status: completed with two reproducible findings; no implementation changes made.

## Frozen target and isolation

- Repository: /Users/pinesky/workspace/amplai-foundry
- HEAD before and after: 6e005a5e3035ef05510c403a7cd691d524b733bd
- Scope: specs/012-portable-document-lifecycle/review-scope-r4.json
- Scope digest: 83d37d8d661b7360be05308af5077cf64d542d2e5a452e1b46d56fbf37118e39
- Scope file SHA256: a9a85f5c5ea745b07320b34e9d7cdad8e66db1ee6bb25674995a11772ad5a01e
- Archive: /tmp/synapse-split-resume.t7Jusk/w003-review-scope-r4-source-snapshot.tar.gz
- Archive SHA256: ad056026489939800efb0df7da7db73f3bc26075d270d774178b2737079496bb
- All 149 source and 391 governing pins matched SHA256, byte size and POSIX mode before and after review. The extracted 540-member archive and disposable checkout also matched all pins before and after execution. Scope digest was independently recomputed using the specified JSON.stringify method.
- Only the three named evaluator projections and derived doc-impact.json were excluded by the supplied scope. Current document validation was executed in the disposable checkout and matched the required semantic snapshot.
- Disposable checkout: /tmp/w003-r4-recovery.d9CWrW/checkout. Separate archive extraction: /tmp/w003-r4-recovery.d9CWrW/archive. No shared checkout writes, cache writes, tests, documentation refresh, Git/Store mutation, real host/fleet/deploy/Vault operations occurred.
- Source imports were verified before tests: amplai_docs and loopv2 resolved under checkout/scripts; amplai_foundry resolved under checkout/src; ai.test_document_lifecycle and its ROOT resolved to the disposable checkout. The /private/tmp spelling is the same macOS filesystem location.

## Findings

### R001 [P1] Dependency classification escapes the reference work budget

Location: scripts/amplai_docs.py:3440, especially the repeated alternative at line 3445. The identical code is installed from tools/amplai-loop-kit/baseline/payload/scripts/amplai_docs.py:3440. Public reachability is scripts/amplai_docs.py:1584 and :2416.

Violated requirements: DOC-001 and FR-008; plan.md's performance and Failure And Recovery guarantees; contracts/portable-operations.md's input-proportional reference scan-work limit and SCOPE_INCOMPLETE outcome. S14's budget is not an end-to-end work bound when its immediate classification consumer can perform unbounded backtracking.

Reproduction: in the existing disposable document fixture, use this 63-byte body:

```python
body = 'Dependencies: ' + ' ' * 30 + 'notes `src/api.py`\n'
```

Both canonical and payload read_document(root, 'docs/guide.md') exceeded the probe's 2-second subprocess deadline. Direct classification with 24 or 30 spaces also exceeded 2 seconds; 12 and 18 spaces completed normally in 0.099 and 0.111 seconds for the whole child process. The exact package installed successfully, its installed amplai_docs.py matched canonical and payload bytes, and the actual installed `loopctl.py docs inventory --release release-2 --security PUBLIC` exceeded 3 seconds with no stdout/stderr. The probe terminated its own timed-out subprocesses.

Mechanism: `(?:[\s,*]+|\band\b|\bor\b|및|와|과)*$` has a repeated variable-length whitespace alternative. A long whitespace suffix followed by ordinary non-declaration prose forces combinatorial backtracking. ReferenceBudget is spent in the lexer and typed extractor, but not during this classification regex. The input is far below all file/count/memory thresholds and produces one ordinary reference; reducing reference count does not resolve it. Increasing the whitespace run increases the unchecked work. Current read/review/selection paths can stall on authored input rather than returning a bounded error, and retrying the same input repeats the stall.

Minimum remediation: replace this suffix test with a linear, non-overlapping declaration parser/matcher and account for classification work within the declared budget. Preserve negation and multi-reference list semantics. Add bounded child-process regressions for near-matching prose through the source, payload and installed public command. No new parser dependency or broad grammar redesign is required.

Proof: /tmp/w003-r4-recovery.d9CWrW/probe-classification.py and /tmp/w003-r4-recovery.d9CWrW/probe-installed.py. Both harnesses exited 0 after recording the failures; their timed-out child operations did not PASS.

### R002 [Blocking-P2] Same-package reinstall loosens a borrowed file's permissions

Location: tools/amplai-loop-kit/install.py:721 and :737, with the unintended write at :749 and receipt replacement at :754.

Violated requirements: FR-003, KIT-002 and the plan's existing-local-state/idempotent-reinstall preservation boundary. The file is explicitly borrowed (`created: false`), not installer-owned content.

Reproduction: create a disposable target with scripts/amplai_docs.py exactly equal to the baseline payload, but mode 0600. Run the canonical installer with `--bootstrap-baseline --app-id recovery-canary --project-id canary --no-git`, without --force or --project-home. First installation exits 0, preserves mode 0600 and records `created: false`, `mode: 384`, `restore: null`. Repeat the exact install command against the unchanged target/package. It exits 0, emits a `composed baseline update` action for this unchanged file, changes its mode to 0644, and replaces the receipt mode with 420. Content SHA256 stays b6db24a3cea54277c7371c0d3a31c6dd936007860b333b97e2a01a6a999b97f6 throughout.

Mechanism: mode is initialized from the package manifest. First-time borrowing replaces it with old_mode. The `previous` branch validates the existing borrowed receipt and copies it, but never restores its validated mode into the desired action. The no-change reinstall therefore treats the package default as a requested permission change and rewrites the user's file. It also loses the original permission value in the receipt. Group/other access is widened even though the user and source made no change. This is a concrete preservation defect, not a claim that the public script contains a secret.

Minimum remediation: preserve the validated prior mode for borrowed baseline files on reinstall/update, or require explicit reconciliation when a different mode is intended. Keep package-mode updates for installer-owned files distinct. Add a first-install/reinstall/uninstall regression starting with an identical borrowed file under a restrictive mode.

Proof: /tmp/w003-r4-recovery.d9CWrW/probe-installed.py; retained fixture /tmp/w003-r4-recovery.d9CWrW/installed-z5sou_j1/target. The later CLI probe replaced only that disposable target's document policy and added disposable guide inputs; it did not change the borrowed script whose permissions were measured. Uninstall was not executed by this probe and is not claimed as observed.

## Executed checks and observations

Interpreter for the following checks: /Users/pinesky/workspace/amplai-foundry/.venv/bin/python, Python 3.11.15. Working directory: /tmp/w003-r4-recovery.d9CWrW/checkout. Source resolution used PYTHONPATH set to that checkout's src, scripts and tests directories; `-B` disabled bytecode writes.

1. Focused command:

   `python -B -m pytest -q -p no:cacheprovider tests/ai/test_document_reference_resources.py tests/ai/test_document_references.py tests/ai/test_document_html_references.py tests/ai/test_document_review.py tests/ai/test_document_preservation.py tests/ai/test_portable_baseline.py tests/ai/test_amplai_kit_installer.py tests/ai/test_amplai_async_runtime.py --junitxml=/tmp/w003-r4-recovery.d9CWrW/focused-pytest.xml`

   Exit 0. JUnit reports 466 tests, 0 failures, 0 errors, 0 skipped, 198.018 seconds. Coverage includes source/payload resource guards, exact URI/line identity, finite HTML input rejection, restrictive-umask atomic publication, concurrent writers, interruption/retry, retirement recovery, baseline installer and terminal/lease presentation. The two findings above are additional probes not covered by these passing assertions.

2. `python -B scripts/loopctl.py docs validate specs/012-portable-document-lifecycle`

   Exit 0, valid=true, RESOLVED, no errors. Snapshot 371a912969c350319dd59fd471418492ffaf02e08c30636025b3e6c0ddd6a3c0. Both configured offline views MATCH. Their input hashes are 2aec6e1ca535dcf700cb1cb7e83ffda2d16d82a6fca75c64fcaea50a1bec0fb8 and 24a1532932db8bffcc9408acaf1e21b7678c98c14cdacbd9085306ba56418437.

3. `python -B scripts/loopctl.py context validate specs/012-portable-document-lifecycle/context-pack.json`

   Exit 0, MATCH, valid=true, 20 matching source entries, no errors. Expected hash d2402f8bb8e63ffea19bdaad17e2235619cfc65aeb87c981bce8b001441086d0.

4. `python -B scripts/loopctl.py trace verify specs/012-portable-document-lifecycle/evidence-trace.jsonl` reported valid=true, 88 events, no errors. `python -B tools/amplai-loop-kit/seal.py --verify` reported ok=true with every stale/missing/mismatched/unlisted collection empty. These ran in one shell cell with a subsequent XML-summary read; the enclosing cell exited 0. Their individual process exit codes were not separately retained.

5. Before/after `node verify-pins.cjs` checks of shared source, extracted archive and disposable checkout exited 0 with 540 matches and zero mismatches each. Scope/archive SHA256 and HEAD remained exact. Original reference requirements/acceptance JSON hashes also matched the declared source pins (5063beb80c88266378c844f1c091d16fae7cea9438f496e6a8caf34cac4bd224 and ee49350982eeaefa11e0369cef3fee152f4e1c647f5557be5e32045d8a1cdce1).

## Evidence read and limitations

Read the repository AGENTS.md, independent code-review skill, relevant permission/test/contract rules, raw work contract/spec/plan/tasks and task-manifest acceptance contracts. Read Knowledge Readiness, the Context Pack, current checkpoint, environment, trace structure/current events, convergence/current local verification and view evidence. Large discovery/acceptance indexes were inspected through focused projections; the 1 MB review registry was not dumped. Read the pertinent D-046/D-053/D-055/D-056 decisions and active claim provenance. Source review covered the document/parser/write/retirement/view paths, loop integration and role filtering, transactional installer recovery, and native terminal/heartbeat diffs with relevant tests.

The existing S08 record reports 2229 passed, four existing live-Slack deselections and complete V2/Kit evidence. That full canonical S08 command, Ruff, mypy, whole Vault lint, package selftest and browser harness were not rerun by this reviewer. Fresh disposable docs validation did validate both configured generated views against their current inputs; it does not replace a new browser execution. No actual Python 3.6, RHEL7, full native Work/resume, real host trust changes, deployment, signing, fleet update, shared Store operation or real retirement is claimed. W004 remains separate and unimplemented. T008 being in_progress is the expected completion-gate state, not a finding.

No prior review finding is adopted as the current verdict. No speculative finding is raised for unsupported target platforms, arbitrary prose DLP, ACL/SELinux/xattrs, or races beyond the stated guarantees. The review is bounded, not an exhaustive proof of every possible input.

Artifacts retained:

- focused-pytest.xml — SHA256 9d5ae60fd3bf382a8f393fd403233f06e283ef38706b0cdbf3e472047a76d4d6
- probe-classification.py — SHA256 bdc0c988fe7d5cb13fa6666fa5a1df9b78717462818cf644a049617463dfad00
- probe-installed.py — SHA256 797efb379c85de2e99c818ce1143cdfec1463334897db1c723d7ae462a9c31fe
- verify-pins.cjs, exact extracted archive, disposable checkout and installed fixture remain under /tmp/w003-r4-recovery.d9CWrW.

VERDICT: CHANGES_REQUIRED

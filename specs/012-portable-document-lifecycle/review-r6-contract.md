# W003 R6 Independent Contract Lens

Counts: P0 0; P1 0; Blocking-P2 0; Advisory 0. No concrete contract defect identified in this review. This is one lens of the frozen local candidate, not original-case completion, the other lenses, native lifecycle or deployment approval.

## Frozen Identity

Repository: /Users/pinesky/workspace/amplai-foundry

Base and HEAD: 6e005a5e3035ef05510c403a7cd691d524b733bd

Before and after: all 155 source and 621 governing files match SHA256, byte size and regular-file mode. Final additional shared hash check: zero mismatches.

- Envelope SHA256: 31d5050daedbaed4f382f694341ec0d139a39bc575cf11e705e7305734b28a94
- Scope SHA256: 61462cceca36c9f4554835729e0ea4a180db6f40e83e8c937358eb16c26a297d
- Archive SHA256: 57c486bd01777fd188d19d46409094dede8b3e63599d543f7af3a08d6c918a6e
- Archive: /tmp/synapse-split-resume.t7Jusk/w003-review-scope-r6-source-snapshot.tar.gz; mode 0600; 777 unique regular members. Listed member bytes, hashes and modes match the envelope.
- Document snapshot: b44fc0bb30a512d1bed7d3e36db2c33d846af96541908809b99d8588ae29d926

Scope check method: Node reads each listed file, compares crypto.createHash('sha256'), buffer.length and fs.lstatSync mode; scope digest hashes JSON.stringify(parsed envelope with scope_sha256 deleted), retaining property order. Python tarfile checked member type, duplicate names, size, mode and streamed content hashes. These checks executed, not merely inspected.

## Executed Evidence

Disposable root: /tmp/w003-r6-contract.yN3Dh6, physically /private/tmp/w003-r6-contract.yN3Dh6. Built by `git archive 6e005a5e3035ef05510c403a7cd691d524b733bd | tar -xf - -C /tmp/w003-r6-contract.yN3Dh6`, followed by extraction of the exact source snapshot. Git provenance was created only there using `git init -q`, `git config core.precomposeunicode true`, `git fetch -q --no-tags /Users/pinesky/workspace/amplai-foundry 6e005a5e3035ef05510c403a7cd691d524b733bd`, and `git reset --mixed -q 6e005a5e3035ef05510c403a7cd691d524b733bd`.

Test command, executed in that root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=/tmp/w003-r6-contract.yN3Dh6/src:/tmp/w003-r6-contract.yN3Dh6/tests:/tmp/w003-r6-contract.yN3Dh6/scripts /Users/pinesky/workspace/amplai-foundry/.venv/bin/python -B -m pytest -q -p no:cacheprovider tests/ai/test_document_context_delivery.py tests/ai/test_document_optional_aliases.py
```

Exit 0; progress reached `[100%]` with no failures. Repository quiet options suppress the usual count summary. Same isolated imports and `--collect-only -q -o addopts='' -p no:cacheprovider` confirmed `120 tests collected in 0.09s`: 32 Context tests and 88 optional-alias tests. This is 120 targeted passing tests, not a rerun of all 2494 tests.

Import assertions confirmed amplai_foundry.__file__ is under the disposable src tree, fixture helper ai.test_document_lifecycle.__file__ under its tests tree, and helper ROOT resolves to the disposable root. The first assertion incorrectly compared logical /tmp to physical /private/tmp and failed; correcting both sides with Path.resolve confirmed isolation. No source or fixture fix was made. Source/payload and installed-CLI tests exercise the isolated files.

Other executed commands and captured outcomes:

- `PYTHONDONTWRITEBYTECODE=1 /Users/pinesky/workspace/amplai-foundry/.venv/bin/python -B specs/012-portable-document-lifecycle/verify-kit-guide-selection.py`: exit 0, PASS_KIT_GUIDE_SELECTION; actual generated/stored records 14/14; three guides and seven mandatory instructions; exact current source hashes; MATCH; Context a66d9e62b971543b077052dbeef92a9f0533eab7c72808fa3d42689e3ab1ab10; retained product reference rejected CROSS_BOUNDARY_REFERENCE.
- `python3 -B scripts/loopctl.py context validate specs/012-portable-document-lifecycle/context-pack.json`: exit 0, valid true, errors [], verdict MATCH, 14 source records MATCH, provenance commit PRESENT.
- `python3 -B scripts/loopctl.py trace verify specs/012-portable-document-lifecycle/evidence-trace.jsonl`: exit 0, events 144, valid true, errors [].
- `python3 -B tools/amplai-loop-kit/seal.py --verify`: exit 0, ok true; baseline_stale, checksum_mismatched, checksum_missing_file, manifest_stale and not_in_checksums all [].
- `python3 -B scripts/loopctl.py readiness evaluate specs/012-portable-document-lifecycle/knowledge-readiness.json`: exit 0, verdict READY, errors []; this validates readiness structure/evidence references, not product completion.
- Isolated stdlib Python called amplai_docs.configured_views(root): valid true, checked 2, status MATCH; developer input b8928e296e5bb6fbce9cf78dc4bc02a60a503f230f17fefccd907509321f80d1, operator input ef613754ab9379d148142405a375831aa672c90cf1eeedad5196c10c467a7d43. It also verified all 20 output/browser evidence artifact hashes and sizes from s08-view-evidence-r6.json.
- `git diff --exit-code 6e005a5e3035ef05510c403a7cd691d524b733bd -- vault src/amplai_foundry schemas .ai-team/app.json .ai-team/local .ai-team/policy/approvals.jsonl`: exit 0, no diff in the reconstructed target. The frozen source inventory contains no forbidden-path change.

## Contract And Evidence Assessment

Reviewed raw work contract, spec, plan and refinements, task-manifest acceptance/commands, original 23-requirement/46-case mapping, readiness/discovery, actual Context, environment, trace, handoff, permissions and applicable repository rules. Inspected actual code/diff against base, particularly installer ownership/transaction boundaries, CLI/runtime presentation, context selection/generation/validation, pre-output reference guards, source/review/release identity and report-only gardening. Source/payload identity is independently seal-checked. S19/S20 acceptance is supported by the targeted execution and real current Context result, rather than the previous review verdict.

Inspected the complete current S08 canonical log and result, S19/S20 canonical outcomes, predecessor revalidation inventory, current attributed source-review and view/native evidence. The recorded S08 run reports 2494 passed, 4 deselected in 588.67 seconds; all 11 v2 checks, outer five checks, seal and selftests succeed. This full run was inspected, not independently rerun. Counts overlap and are not summed. Native evidence remains explicitly transport-only. Original cases retain zero final PASS, consistent with pending completion gates.

The disposable catalog reports 21 verified and four stale retained guides (.ai-team/README.md, .ai-team/knowledge/README.md, AGENTS.md, README.md). Only their declared_reference_sha256 differs from the recorded review snapshots; ignored local runtime references such as .amplai/tmp/ and .amplai/runtime/governance.db are absent from the isolated reconstruction. No live runtime data was copied to fabricate parity. Three current Kit guides and both immutable views reproduce MATCH independently.

The parent reported rerunning owning-root docs impact/validate after regenerating only excluded doc-impact.json to incorporate envelope membership: exit 0, RESOLVED, errors [], unchanged semantic snapshot and both views MATCH. This review independently READ the resulting shared projection: status RESOLVED, scan_complete true, 25/25 handled documents, zero unresolved references and snapshot b44fc0bb30a512d1bed7d3e36db2c33d846af96541908809b99d8588ae29d926. The owning-root validation command was parent-executed; it is not represented as this reviewer's execution. No frozen source/governing file changed.

## Limits

No source mutation test, complete full-suite rerun, browser execution, live host/network operation, Python 3.6/RHEL7 runtime, full native Work/resume, signing, fleet, deployment or W004 packaging verification was performed. Browser artifacts and complete canonical execution evidence were inspected and hash-checked; local views were regenerated in memory for validation. No shared checkout, cache, Git state, Project Store, Vault or other repository was modified by this reviewer. Disposable copies and this report are retained for inspection.

VERDICT: PASS

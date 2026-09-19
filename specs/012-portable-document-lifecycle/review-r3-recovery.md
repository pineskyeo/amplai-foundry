# W003 R3 — Independent Failure/Recovery Review

Scope: the second sequential R3 lens, read-only against all shared checkouts. No current R3 peer conclusions were read. Findings: **P0 0 / P1 1 / Blocking-P2 0 / Advisory 0**.

## Finding R3-REC-001 — Unbudgeted per-reference prefix copies can exhaust memory

- **Severity / lens:** P1 / failure-recovery.
- **Location:** `scripts/amplai_docs.py:2727`; identical installed source at `tools/amplai-loop-kit/baseline/payload/scripts/amplai_docs.py:2727`. Caller reaches this before disclosure in `scripts/amplai_docs.py:1761` and dependency extraction in `scripts/amplai_docs.py:3126`.
- **Violated contract:** bounded reference scanner in `specs/012-portable-document-lifecycle/contracts/portable-operations.md:60`; configured resource limits must yield incomplete/error in `plan.md:35`; S12 bounded tokenizer guidance in `task-manifests/PORTABLE-DOC-T012.yaml:82`.
- **Mechanism:** every token copies `text[line_start:start]` into `prefix`. The character-work budget and 10,000-token limit do not account for those retained copies. Multiple links on one long line therefore require quadratic memory before security selection can finish. Input byte limits do not bound this amplification.
- **Reproduction:** `probe_recovery.py` imports the exact frozen production module, checks its location and SHA256, and scans valid links on a single line. At 250 / 500 / 1,000 links, input sizes are 7,250 / 14,500 / 29,000 bytes; retained prefixes total 902,625 / 3,617,750 / 14,485,500 bytes. Measured Python allocation peaks are 1,038,500 / 3,885,987 / 15,020,877 bytes. All scans complete successfully. The actual public `select_documents` operation also accepts the 1,000-link source under a 1 MiB file limit.
- **Impact:** a source below the read limit can consume gigabytes during ordinary review/query/build. From the exact copy formula, 10,000 of these links produce 1,449,855,000 prefix bytes from 290,000 input bytes; a long leading line prefix increases this further. That larger case is a code-derived extrapolation, **not an executed OOM test**. Such input can terminate the workflow instead of producing its promised bounded failure.
- **Smallest credible remediation:** retain line/start offsets rather than copying every prefix. Adapt dependency context checks to those offsets or charge any materialization to an aggregate budget before allocating. Add a long-single-line regression that proves bounded growth or a non-disclosing `SCOPE_INCOMPLETE`, including the canonical payload and a real consumer.

## Evidence and boundaries

Exact commands and exit codes are in `commands.json`. Executed targeted tests passed **94 cases**: 84 atomic/retry/interruption/reference/retirement checks and 10 installer rollback/restart/conflict checks. Trace verification passed all 55 events. The independent directory-fsync fault probe surfaced an error after publication, retained a readable canonical record and its mode, left no pending file, and exact retry resolved; it is not reported as another defect.

Read raw contract/spec/plan, all 13 task contracts and generated tasks, readiness/discovery, authorization/refinement, acceptance map, both current Context artifacts, trace, relevant rules/permissions and active claims; inspected current implementation and installer diff. Current retained proof is S12, S13, all nine R3 revalidations, S08 R3 canonical verification/convergence, guide review r6 and R3 native/view evidence. The retained full suite reports **2,049 passed, four live Slack tests deselected**; this lens did not rerun that suite. Native proof is transport only. Python 3.6 execution, RHEL7, full native Work/resume, W004 packaging/activation and production deployment remain unverified or outside this review.

No shared source, governing artifact, projection or trace was edited. No live Store, host, Slack, trust/config, production or network operation ran. Fixtures and reports remain only under this temporary review directory. The sole process monkeypatch was restored. Real credential/PII values are absent from this report and reproduction outputs.

## Frozen-scope integrity

Both checks returned exit 0: **147 source + 231 governing = 378 rows**, each checked for SHA256, bytes and octal mode. No mismatches. Only the four manifest-declared derived projections are excluded; none was refreshed.

- HEAD: `6e005a5e3035ef05510c403a7cd691d524b733bd`.
- Scope: `883bc9e39e7a83739121f3a40c327af99bd3d85399483c100a83db32c6bb9a5c`.
- Scope file: `56f56a1c242dae26bd1ab67515be37ca5c87d0bd28e0dd2acae1d358b8e484bb`.
- Semantic document snapshot: `92fd8514805893ae39bc8629cdad53af2b3797678232506dbf53150f505e8349`.
- Preserved archive: `/tmp/synapse-split-resume.t7Jusk/w003-review-scope-r3-source-snapshot.tar.gz`; 1,015,271 bytes; mode `0600`; SHA256 `3577e29210b89058a34b5d1754677695b259f02635641688c91b4390495937e8`. Verified before and after.
- `scope-before.json`: `e95fc23081ab547e8d134f5dfccd09041e5e78c4616590eca754307f040acc0e`.
- `scope-after.json`: `73ed4e4eb4ed55d505b3d53f28b8ef4e23e715a49a55d28a5d8749e3ff46a8d5`.
- `probe_recovery.py`: `a41f361686e966edc2fb2a8f86544eaddcf081f17d5e9239a4dc6a138ec028fd`.
- `probe-recovery.json`: `9f793469c9654102b451f6eff6788b684c11486f18859150de2ce42badbe4584`.

VERDICT: CHANGES_REQUIRED

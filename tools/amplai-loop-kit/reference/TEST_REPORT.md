# Verification Report

This file keeps historical evidence and defines compatibility gates for each candidate.
Do not reinterpret an old target repository result as evidence for a new release.

## 2.5.0 candidate gate

The candidate extends the existing installer and Work/Design controller. A version label,
checksum alone, or a source-only test is not release verification.

Required local checks include the complete package seal; fresh baseline and existing-app
installation; repeat, update, rollback and uninstall preservation; source/payload/installed
capability parity; actual installed Work/Design context and Slice evaluation; document
selection, full impact and attributed review; a normal code change through offline HTML
rebuild and drift rejection; lossless source-section preservation and fixture-only
retirement/recovery; terminal Store read-only views and credential-safe heartbeat output.

Run the repository's full verification profile, the package selftest, and sequential
contract, failure/recovery and regression reviews. Preserve exact candidate and log hashes.
The package must not copy source-repository domain rules, private state or host trust.

An available host's constant-answer foreground canary proves response transport only.
Native full Work lifecycle, oldest supported Python runtime, RHEL7, real fleet installation
and production activation each need their own evidence. Do not infer them from local tests.

## 2.3.2 required gate

Run against the packaged payload and an installed target repository.

```text
- package checksum/selftest
- installer fresh install, update, idempotence, uninstall
- pre-existing Claude and Codex hook preservation
- Codex hook creation and created-directory cleanup
- Claude command/session resume construction
- Codex exec --json command/thread resume construction
- JSONL thread.started.thread_id recovery
- prompt redaction for both native runners
- SessionStart/SessionEnd output for both hosts
- unattended dangerous-permission warning for both hosts
- shared skill SSOT and exact Claude symlink mirror
- internal Codex skill allow_implicit_invocation=false
- loopctl doctor
- at least one real foreground CLI smoke per enabled host before auto_start
```

A deterministic command adapter test proves protocol handling but does not prove that an installed native
CLI, account, sandbox and approval configuration can complete a real Work. Record that as separate release
evidence.

## Historical 2.2.0 record

- Kit version: `2.2.0`
- Runtime protocol: `amplai.async-cross-app.v1`
- Target repository: `synapse`, branch `fix/amplai-loop-kit-2.2.0`
- Python used: `3.9.6`
- Target compatibility: Python `3.6+`, standard library only

```text
tests/ai/test_amplai_async_runtime.py      16 PASS
tests/ai/test_amplai_kit_regressions.py    27 PASS
tests/ai/test_amplai_kit_installer.py      10 PASS
                                           53 PASS

tools/amplai-loop-kit/selftest.py          10 checks PASS
```

Historical coverage included content-hash tamper detection, evidence/authority gates, Work state transitions,
dependency/cycle validation, lease expiry and host boundary, retry backoff, event-chain integrity, reseal,
policy drift, permission-bypass warnings, installer rollback and uninstall, generated handoff, Claude session
continuity, and a Cortex → Synapse → Cortex async round trip.

The historical report did not invoke a model. Its command adapter, result parsing and async round trip were
deterministic. Native worker permissions therefore still required a foreground smoke before `auto_start`.

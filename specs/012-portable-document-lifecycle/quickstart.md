# Validation Quickstart

Status: S01-S07 implemented and locally verified; S08 integration is in progress.
Commands below are executable. Do not execute installer examples against real apps.

## Prerequisites

Use disposable fixture directories, Python/Git/Bash and existing Foundry `.venv` for its
full suite. Portable installed tooling targets Python 3.6-compatible stdlib behavior.
The available verifier is Python 3.11.15 on Darwin arm64; portable commands also ran on
system Python 3.9.6. S02 recorded actual Claude Code/Codex foreground constant-answer
transports, not a native Work lifecycle. Python 3.6 and RHEL7 remain unverified.
Browser checks use an isolated profile and synthetic input.

## Existing Baseline Commands

These commands already exist and do not install the Kit into another repository:

```sh
python3 -B tools/amplai-loop-kit/seal.py --verify
python3 -B scripts/loopctl.py doctor
.venv/bin/python .ai-team/verifiers/run.py --profile v2
```

The unchanged initial baseline passed 1,625 tests. S06's full V2 passed 1,849 tests,
with four live Slack cases deselected in both runs. S07's canonical 35 and separate
63-test regression passed. S08's current canonical evaluator passed 1,880 tests,
four live Slack cases were deselected, and all 12 installer selftests passed. Exact inputs
and logs are retained in s08-local-verification.json and s08-canonical-eval.log.
Two current offline guides passed source/output validation and isolated browser checks
in s08-view-evidence.json. Independent completion reviews remain a separate gate.
The prior slice passes are historical observations, not current candidate approval.

## Fresh Repository Canary

The portable test harness creates an empty synthetic repository and installs the explicit
baseline-plus-extension profile using the existing installer. It runs doctor, contract,
readiness, context, JSON task validation/generation, a real Slice evaluator, docs and
report-only gardening with the parent source unavailable. A marker-only stub must fail.

Repeat installation, introduce owned and unrelated local edits, update/uninstall, omit the
profile on update, inject transaction failures and check exact recovery. Missing checksum
entries, extra payloads, symlink substitution and duplicate path composition must reject.
Existing host settings, user hooks, domain rules and Store binding sentinels must survive.

## Document And View Canary

Use at least 41 changed sources, rename/delete cases, document-only contract edits,
new build-tool paths, configured ontology pins and inaccessible required dependencies.
Require exact expected/processed scope equality. Submit individually justified reviews,
then change one input and require rejection without losing the old published report.

Create same-topic documents for two releases and multiple security/lifecycle/freshness
states. Duplicate current canonical records block. Verify current and explicit history
selection before titles/snippets. Build external output only from a separate approved
synthetic input bundle and scan every output file for private sentinel absence.

Build twice from identical input and compare all bytes. Modify generated output and require
drift rejection. Open in an isolated browser with network and JavaScript disabled; test
headings, skip links, keyboard focus, narrow tables and print. Script-like source/title/code
must render as text. Latest cannot advance to an unverified release set.

## Preservation And Work Canary

Map all significant source sections to retained or destination records. Unknown sections,
retention holds, unverified external references and user backups block retirement. Exercise
approved quarantine/recovery only on synthetic files; changed target or approval must reject.
Normal real-repository gardening remains report-only throughout.

In a disposable Project Store, complete a Work and read JSON/Markdown/handoff/show. Reads
must not change Store bytes/mtime; Markdown shows own results without reactivation advice.
Heartbeat output has no synthetic credential while the same credential still renews and
completes the Work. Never call a real supervisor, Slack connector or production service.
A separately recorded native foreground canary may use an available host under existing
authority, with synthetic prompts, isolated input and no automatic Work activation.

## Completion

Generated tasks and retained evaluator input bind each scenario to its command and evidence.
After full V2 verification: converge, current document freshness, three sequential independent
reviews, report-only gardening, security review and native Work result. Unrun old-interpreter,
native full-lifecycle and target checks remain separate NOT VERIFIED fields in the HTML delivery.

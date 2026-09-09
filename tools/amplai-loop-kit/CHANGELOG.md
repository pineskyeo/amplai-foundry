# Changelog

## 2.4.0

Host abstraction and Platform federation boundary.

- Extracted Claude Code, Codex, and generic command execution into `amplai_hosts.py`; Supervisor and lifecycle hook no longer own provider-specific argv/session logic.
- Preserved `/work` for Claude Code and `$work` for Codex while sharing the same durable Work contract.
- Added non-destructive Decision/Evidence promotion metadata and fail-closed lifecycle: `LOCAL → CANDIDATE → SUBMITTED → ACCEPTED/REJECTED`.
- Added deterministic promotion envelopes with origin provenance and idempotency keys for Platform 0.4.
- Added host-adapter and federation regression suites and included them in the distributable payload.
- Added additive Work controller, runner profile, fixed base ref, and request reference fields. The Supervisor now
  dispatches a Work in a managed detached worktree and emits host-specific `/work` or `$work` prompts.
- Slack/Hermes activation remains disabled by default and is deliberately outside Hermes credentials: a separate
  signed human activation boundary and durable Work status outbox preserve Project Store authority.

## 2.3.2

First-class Codex compatibility and shared-skill ownership correction (`D-055`).

- Added a native `codex` supervisor runner using `codex exec --json`, explicit
  session resume, JSONL `thread.started.thread_id` recovery, `$work` prompting,
  and prompt redaction in run metadata.
- Added non-destructive project-local Codex `SessionStart`/`SessionEnd` hook
  installation in `.codex/hooks.json`; existing hooks and unrelated fields are
  preserved and uninstall removes only AMPLAI-owned handlers.
- Generalized the hook adapter for Claude Code and Codex. Codex receives the same
  Project Store context without Claude-only `sessionTitle`/environment export.
- Made `.agents/skills/` the shared canonical skill tree and converted every
  `.claude/skills/*` entry to a symlink mirror. Internal Codex capabilities now
  set `allow_implicit_invocation: false` so development requests enter through
  `$work` or `$design` rather than bypassing the controller.
- Added Codex unattended-safety warnings for `danger-full-access`, `--yolo`, and
  approval/sandbox bypass flags.
- Added runtime, installer, hook, JSONL-resume, permission, mirror-layout, and
  uninstall regressions.

## 2.3.1

Test isolation fix found deploying to a third application (`D-054`).

- `AmplaiHookTest` builds a temporary Project Store, but `discover_project_home()`
  reads `AMPLAI_PROJECT_HOME` before any repository-local binding — and the kit's
  own SessionStart hook exports that variable into the session.  So in any
  repository where the kit is installed, running the suite from an agent session
  made the hook test read the *real* Store and fail, while running it from a bare
  terminal passed.  Where that test is wired into a blocking verifier check, the
  whole gate failed for a reason that had nothing to do with the change under test.
  The hook's precedence is intentional and is unchanged; the test now clears every
  `AMPLAI_*` variable in `setUp` and restores it in `tearDown`.
- Added regression coverage: one test runs the hook test with a decoy Store named in
  the environment, another pins the precedence itself so the reason stays visible.
  Both live in `test_amplai_kit_regressions.py`.
- That regression file now puts its own directory on `sys.path`.  Applications differ
  on whether `tests/ai` is a package, and the sibling import broke under some runners.

## 2.3.0 (in progress)

Ownership moved to amplai-foundry (`D-053`); see `PROVENANCE.md`.

- The supervisor now takes a single-instance lock in the Project Store itself, so a
  second supervisor is refused no matter which copy of the script starts it.  The
  installer places a `supervisor/` entry point in the Store.
- Distribution targets are configured in two layers: a committed logical list and a
  host-local path map, so absolute paths never enter version control.
- Documentation no longer presents one particular application as the reference host.

## 2.2.0

Correctness and operability fixes found reviewing 2.1.0.

- Project lock: a holder no longer deletes a lock it lost to a stale-reclaim,
  and stale locks are reclaimed with one atomic rename so only one contender
  wins.  2.1.0 could leave two processes believing they held the lock.
- Child ids (`CR-...-W001`) resolve against the change directory that actually
  exists, so a `change_id` containing `-W`/`-Q`/`-D`/`-E` no longer writes and
  reads different paths.  A `change_id` shaped like a child id is rejected.
- Added the escape hatches the protocol already described: `work cancel`
  (with `--cascade`), `change cancel`, `work retarget`, `work reset-attempts`
  and `project reseal`.  `CANCELLED` was previously unreachable and an
  exhausted dependency deadlocked everything downstream with no recovery.
- Retry backoff: a Work that fails or loses its lease waits
  `retry_backoff_seconds` (doubling, capped) before it can be claimed again.
- Supervisor waits on worker completion instead of rescanning the store four
  times a second, which had starved the workers' own lock acquisitions.
- Event chain appends read only the last event instead of re-parsing the whole
  file, removing the quadratic cost of a long-lived store.
- Lease tokens are no longer printed by `work claim` or required on argv;
  `scripts/amplai.py` reads `AMPLAI_LEASE_TOKEN` from the environment.
- `.ai-team/AUTONOMY_POLICY.md` is created when missing instead of blocking the
  install, so Loop V2 apps without that file are valid targets.
- Installer reports central policy drift, rolls back a Project Store it created,
  refuses to silently downgrade, and supports `--uninstall`.
- Leases are host-local, so Work claimed on another host is no longer recovered
  here; `project verify` reports it instead.
- `project verify` reports which prohibitions are machine-enforced and which are
  worker prompt guidance only, and warns when an auto-start app bypasses
  permission checks.

## 2.1.0

- Added machine-enforced Question, Evidence, and Decision objects.
- Added AUTO, CHALLENGE, and HUMAN authority policy.
- Added central Git Project Store with CR, Contract, Work, dependency, hash, and event-chain state.
- Added deterministic Local Supervisor with claim, lease, heartbeat, recovery, and app capacity.
- Added Claude Code non-interactive worker and SessionStart/SessionEnd adapters.
- Added generated target-app handoff views; handoff prose is no longer canonical state.
- Added non-destructive, idempotent install/update with owned-file hashes, marker merges, hook merges, backup, and rollback.

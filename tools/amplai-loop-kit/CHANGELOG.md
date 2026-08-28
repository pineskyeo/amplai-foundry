# Changelog

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

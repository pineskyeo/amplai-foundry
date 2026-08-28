# Security and Recovery Notes

- Lease tokens are host-local. Supervised workers receive them through
  `AMPLAI_LEASE_TOKEN` and `scripts/amplai.py` reads that variable, so the token
  never needs to appear on a command line. `work claim` redacts it unless
  `--print-token` is given; `--token-file` writes it to a `0600` file.
- Claude prompts are replaced with `<prompt>` in command logs.
- command/stdout/stderr run artifacts are mode `0600`; run directories are mode `0700`.
- repository paths, runner command, leases, sessions, and run logs live under ignored local state.
- Project Store objects have canonical content hashes; event history has an append-only hash chain.
- Work completion requires evidence and rejects unresolved OPEN Questions.
- HUMAN Questions cannot be deferred to bypass approval.
- `HUMAN_REQUIRED` requires a linked HUMAN Question and cannot reactivate until it is resolved.
- Installer never raises Claude permissions and never replaces unrelated hooks.
- Installer preflights all target changes. Existing changed files are backed up; target writes roll back on failure.
- The Supervisor process itself cannot merge, push, deploy, release, or create
  goals/contracts. It only launches the configured worker: whatever that worker
  is permitted to do is governed by the runner args, not by this kit.
- `policy.forbidden_automatic_actions` is mostly worker prompt guidance.
  `project verify` lists exactly which entries the Runtime enforces.
- Leases are host-local while `changes/` is shared through Git. Work claimed on
  another host is never auto-recovered here; `project verify` reports it.

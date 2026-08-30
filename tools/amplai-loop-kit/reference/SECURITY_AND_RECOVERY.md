# Security and Recovery Notes

- Lease tokens are host-local. Supervised workers receive them through `AMPLAI_LEASE_TOKEN`; the token
  does not need to appear on argv. `work claim` redacts it unless `--print-token` is given.
- Claude and Codex prompts are replaced with `<prompt>` in command logs.
- command/stdout/stderr run artifacts are mode `0600`; run directories are mode `0700`.
- repository paths, runner command, leases, sessions/threads, and run logs live under ignored local state.
- Project Store objects have canonical content hashes; event history has an append-only hash chain.
- Work completion requires evidence and rejects unresolved OPEN Questions.
- HUMAN Questions cannot be deferred to bypass approval.
- `HUMAN_REQUIRED` requires a linked HUMAN Question and cannot reactivate until it is resolved.
- Installer never raises Claude permissions or Codex sandbox/approval policy.
- Installer preserves unrelated hooks in `.claude/settings.json` and `.codex/hooks.json`; uninstall removes
  only handlers recorded as AMPLAI-owned. Project-local Codex hooks still require explicit review/trust in `/hooks`.
- Auto-start apps using Claude permission bypass or Codex `--yolo`, approval/sandbox bypass, or
  `danger-full-access` produce a `project verify` warning.
- Installer preflights all target changes. Existing changed files are backed up; target writes roll back on failure.
- The Supervisor cannot merge, push, deploy, release, or create goals/contracts. It only launches the
  configured worker; effective permissions are determined by runner args.
- `policy.forbidden_automatic_actions` is partly machine-enforced and partly worker guidance.
  `project verify` reports the distinction.
- Leases are host-local while `changes/` is shared through Git. Work claimed on another host is never
  auto-recovered here; `project verify` reports it.

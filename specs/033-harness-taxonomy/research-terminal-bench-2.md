# Terminal-Bench 2.0 For Corpus v2 (checked 2026-09-30)

- Source: https://github.com/harbor-framework/terminal-bench-2 (Apache-2.0, `gh api` license field);
  Hugging Face mirror https://huggingface.co/datasets/harborframework/terminal-bench-2.0 (apache-2.0, read-only mirror).
- 89 task directories. Each: `instruction.md`, `task.toml`, `environment/` (Docker build), `solution/`, `tests/`.
- `task.toml` (example `cancel-async-tasks`): `[metadata]` difficulty, category, tags, expert/junior time estimates;
  `[verifier] timeout_sec`; `[agent] timeout_sec`; `[environment]` `docker_image` (a pinned per-task image),
  cpus, memory_mb, storage_mb, gpus, `allow_internet`.
- Scan of all 89 `task.toml` files (`gh api`, 2026-09-30):
  - difficulty: medium 55, hard 30, easy 4;
  - category: software-engineering 26, system-administration 9, scientific-computing 8, security 8,
    data-science 8, debugging 5, file-operations 5, model-training 4, mathematics 4, data-processing 4,
    machine-learning 3, games 1, personal-assistant 1, optimization 1, data-querying 1, video-processing 1;
  - `allow_internet = true` for all 89; `gpus = 0` for all 89.

## Fit With V3

- V3 runs the driver inside a container whose egress is limited to the provider allowlist
  (`egress_containment`, D-073/D-091). TB2 declares internet for every task; whether a task needs it at run time
  is unknown per task.
- Adapter rule (no egress change): build a per-task worker image from the task's `docker_image` plus the pinned
  driver layer, run the reference `solution/` under V3's containment, and admit only tasks whose reference passes the
  task's `tests/` there and whose unmodified environment fails them (fairness). Tasks that need the internet at run
  time drop out.
- Per-task images are large; the adapter admits a subset (target about 40) chosen for domain spread.

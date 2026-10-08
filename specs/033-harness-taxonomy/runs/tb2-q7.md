# TB2 Q7: Test Entry And Working Directory Per Task (2026-10-08)

Answers the first half of interfaces §14 Q7 from the task files only. No task image was pulled and
no admission ran, so the admission count, whether the driver layer runs on each task base image and
the image sizes stay 확인 필요.

## Sources

- Terminal-Bench 2.0: `harbor-framework/terminal-bench-2` cloned read-only at commit
  `2fd12b88aafdd04a52c298e3940bcb189f9766d6` (2026-04-29, "Add task metadata to task manifests").
  `LICENSE` is the Apache License 2.0 text (first lines "Apache License / Version 2.0, January 2004 /
  http://www.apache.org/licenses/"), the text `tb2.check_license` accepts.
- Harbor, the runner TB2 names (`README.md`: `uv tool install harbor`, `harbor run --dataset
  terminal-bench@2.0`): `laude-institute/harbor` cloned read-only at commit
  `4d1dcfb25470f66daa3b3a169e061b55ab75ce45` (2026-10-07), Apache-2.0. This Harbor commit is newer
  than the TB2 commit; the conventions below are cross-checked against the TB2 files themselves
  (every `test.sh` names `/tests/` and `/logs/verifier/reward.txt`). Which Harbor version TB2 was
  authored against is 확인 필요.
- Scan: a script over every `<task>/task.toml`, `tests/`, `solution/` and `environment/Dockerfile`
  (operator scratch `tb2q7/scan.py`, output `tb2q7/facts.json`; not committed).

## Counts (89 Task Directories)

| Fact | Count | Source |
|---|---|---|
| task directories with `task.toml` | 89 | clone |
| `tests/test.sh` present | 89 / 89 | `<task>/tests/test.sh` |
| `test.sh` first line `#!/bin/bash` | 88 / 89 (`llm-inference-batching-scheduler` has it on line 3, after the canary comment) | first line |
| `test.sh` writes `echo 1 > /logs/verifier/reward.txt` and `echo 0 > /logs/verifier/reward.txt` | 89 / 89 | `test.sh` |
| `test.sh` writes `reward.json` | 0 / 89 | `test.sh` |
| `test.sh` uses `set -e` | 0 / 89 | `test.sh` |
| last statement of `test.sh` is the `if … fi` that writes the reward | 89 / 89 | `test.sh` |
| `test.sh` refers to `/tests/` | 87 / 89 | `test.sh` |
| `test.sh` guard `if [ "$PWD" = "/" ]; then … exit 1` (before any reward is written) | 87 / 89 (not `hf-model-inference`, `kv-store-grpc`) | `test.sh` |
| `test.sh` installs packages: `uv`/`uvx`/`pip install` | 89 / 89 | `test.sh` |
| `test.sh` downloads `uv` with `curl … astral.sh/uv/…/install.sh` | 82 / 89 | `test.sh` |
| `test.sh` runs `apt-get`/`apt` update or install | 82 / 89 (not 7: `build-cython-ext`, `configure-git-webserver`, `fix-code-vulnerability`, `hf-model-inference`, `kv-store-grpc`, `largest-eigenval`, `reshard-c4-data`) | `test.sh` |
| `task.toml` `[verifier]` keys | `timeout_sec`, `env` (89 / 89); no entry command key | `task.toml` |
| `task.toml` sets `[environment] workdir` | 0 / 89 | `task.toml` |
| `environment/Dockerfile` present | 89 / 89 | |
| Dockerfile `USER` | 0 / 89 | Dockerfile |
| Dockerfile last `WORKDIR` | `/app` 86, `/app/personal-site` 1 (`fix-git`), `/app/dclm` 1 (`sanitize-git-repo`), `/workspace` 1 (`prove-plus-comm`) | Dockerfile |
| Dockerfiles with 2 `WORKDIR` lines | 7 (see table) | Dockerfile |
| `solution/solve.sh` present | 89 / 89 | `<task>/solution/` |
| `solve.sh` refers to `/solution` | 4 / 89 | `solve.sh` |
| `[environment] docker_image` | 89 distinct, all `alexgshaw/<task>:20251031`, none digest-pinned | `task.toml` |
| `allow_internet = true` / `gpus = 0` | 89 / 89 each | `task.toml` |
| `[verifier] timeout_sec` | 900 s 48, 1800 s 17, 3600 s 12, 1200 s 6, 2400 s 2, 600 / 360 / 7200 / 12000 s 1 each | `task.toml` |

## The Test Entry

- Command: `/tests/test.sh`. Harbor uploads the task's `tests/` to `/tests`
  (`harbor/verifier/verifier.py:175-183`; `EnvironmentPaths.tests_dir = /tests`,
  `harbor/models/trial/paths.py:44`), discovers `tests/test.sh` (`harbor/models/task/paths.py:152-154`),
  runs `chmod +x` as root, then executes the script path with stdout and stderr redirected to
  `/logs/verifier/test-stdout.txt` (`verifier.py:205-232`, `harbor/utils/scripts.py:122-159`).
  `task.toml` names no entry command (`[verifier]` holds only `timeout_sec` and `env`).
- Verdict: the reward file, not the exit status. Harbor reads `/logs/verifier/reward.json` if it
  exists, else `/logs/verifier/reward.txt` as one float (`verifier.py:67-87,257-266`), and raises
  when neither exists. Every TB2 `test.sh` writes `1` when its pytest run exits 0 and `0` otherwise
  (`if [ $? -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > …; fi`; one task,
  `fix-code-vulnerability`, requires two pytest runs to pass). Because that `if … fi` is the last
  statement and no script sets `-e`, the script's exit status is the `echo`'s, so a failing task
  still exits 0. Grading by exit status would score every TB2 task as passing.
- Working directory: the container's working directory. Harbor's docker exec passes `-w` only when
  `cwd` or `[environment] workdir` is set (`harbor/environments/docker/docker.py:1362,1411-1412`);
  no TB2 `task.toml` sets `workdir`, so the image's `WORKDIR` applies: `/app` for 86 tasks (table).
  87 scripts refuse to run when `$PWD` is `/`.
- User: Harbor runs the script as the environment's default user (`verifier.py:227-232`); no TB2
  Dockerfile sets `USER`, so the default user is the image's (root unless the base image sets one,
  확인 필요 per image).
- Timeout: `[verifier] timeout_sec` (all 89 set it).

## What This Means Under V3 Containment (§10.5: network none, uid 65534, read-only root)

- The entry itself is known for 89 / 89 tasks: `bash /tests/test.sh`, verdict from
  `/logs/verifier/reward.txt` (implemented as `meta_harness/tb2_grading.py`). `bash` instead of
  `chmod +x` and direct execution: `/tests` is mounted read-only, and one script has no shebang on
  its first line.
- Whether the scripts can pass offline is 확인 필요 per task: all 89 install test dependencies at
  test time (`uv`/`uvx`/`pip`), 82 download `uv` with `curl`, 82 run `apt-get` (which needs root).
  Without network those steps fail; whether the image already holds what they install is not in the
  task files. The scripts do not stop on a failed step (no `set -e`), so a failed install shows up
  as reward `0` (or no reward when the script stops earlier). Only the operator admission run
  (§10.5 step 4, 3 tasks first) can tell how many tasks still admit; a task whose reference solution
  does not pass offline drops out (step 4 (c)).
- The working directory per task is the Dockerfile's last `WORKDIR` (table); the authoritative value
  is the built image's `Config.WorkingDir` (`scripts/tb2_adapter.py workdir`, needs the image).
- `solve.sh`: Harbor uploads `solution/` to `/solution` (`harbor/agents/oracle.py:93-105`,
  `paths.py:45`); 4 scripts name `/solution`, which the S7a admission mounts at
  `/amplai-input/solution`. The solution command stays an operator argument.

## Image List (Not Pulled)

89 images, one per task, `alexgshaw/<task>:20251031` (the table's second column). Total image size:
확인 필요 — neither the TB2 repository nor `task.toml` states image sizes (`task.toml` states only
the container limits `storage_mb = 10240`, `memory_mb`, `cpus`).

## Per Task

| Task | `docker_image` | Dockerfile `WORKDIR` (in order) | verifier timeout (s) | notes |
|---|---|---|---|---|
| `adaptive-rejection-sampler` | `alexgshaw/adaptive-rejection-sampler:20251031` | `/app` | 900 | — |
| `bn-fit-modify` | `alexgshaw/bn-fit-modify:20251031` | `/app` | 3600 | — |
| `break-filter-js-from-html` | `alexgshaw/break-filter-js-from-html:20251031` | `/app` | 1200 | — |
| `build-cython-ext` | `alexgshaw/build-cython-ext:20251031` | `/app` | 900 | no apt, no curl/wget |
| `build-pmars` | `alexgshaw/build-pmars:20251031` | `/app` | 900 | — |
| `build-pov-ray` | `alexgshaw/build-pov-ray:20251031` | `/app` | 12000 | — |
| `caffe-cifar-10` | `alexgshaw/caffe-cifar-10:20251031` | `/app` | 1200 | — |
| `cancel-async-tasks` | `alexgshaw/cancel-async-tasks:20251031` | `/app` | 900 | — |
| `chess-best-move` | `alexgshaw/chess-best-move:20251031` | `/app` | 900 | — |
| `circuit-fibsqrt` | `alexgshaw/circuit-fibsqrt:20251031` | `/app` | 3600 | — |
| `cobol-modernization` | `alexgshaw/cobol-modernization:20251031` | `/app` | 900 | — |
| `code-from-image` | `alexgshaw/code-from-image:20251031` | `/app` | 1200 | — |
| `compile-compcert` | `alexgshaw/compile-compcert:20251031` | `/app` | 2400 | — |
| `configure-git-webserver` | `alexgshaw/configure-git-webserver:20251031` | `/app` | 900 | no apt |
| `constraints-scheduling` | `alexgshaw/constraints-scheduling:20251031` | `/app` | 1200 | — |
| `count-dataset-tokens` | `alexgshaw/count-dataset-tokens:20251031` | `/app` | 900 | — |
| `crack-7z-hash` | `alexgshaw/crack-7z-hash:20251031` | `/app/john/src` → `/app` | 900 | — |
| `custom-memory-heap-crash` | `alexgshaw/custom-memory-heap-crash:20251031` | `/build` → `/app` | 1800 | — |
| `db-wal-recovery` | `alexgshaw/db-wal-recovery:20251031` | `/app` | 900 | — |
| `distribution-search` | `alexgshaw/distribution-search:20251031` | `/app` | 3600 | — |
| `dna-assembly` | `alexgshaw/dna-assembly:20251031` | `/app` | 1800 | — |
| `dna-insert` | `alexgshaw/dna-insert:20251031` | `/app` | 1800 | — |
| `extract-elf` | `alexgshaw/extract-elf:20251031` | `/app` | 900 | — |
| `extract-moves-from-video` | `alexgshaw/extract-moves-from-video:20251031` | `/app` | 1800 | — |
| `feal-differential-cryptanalysis` | `alexgshaw/feal-differential-cryptanalysis:20251031` | `/app` | 1800 | — |
| `feal-linear-cryptanalysis` | `alexgshaw/feal-linear-cryptanalysis:20251031` | `/app` | 1800 | — |
| `filter-js-from-html` | `alexgshaw/filter-js-from-html:20251031` | `/app` | 1800 | — |
| `financial-document-processor` | `alexgshaw/financial-document-processor:20251031` | `/root` → `/app` | 1200 | — |
| `fix-code-vulnerability` | `alexgshaw/fix-code-vulnerability:20251031` | `/app` | 900 | no apt, no curl/wget |
| `fix-git` | `alexgshaw/fix-git:20251031` | `/app` → `/app/personal-site` | 900 | — |
| `fix-ocaml-gc` | `alexgshaw/fix-ocaml-gc:20251031` | `/app` | 3600 | — |
| `gcode-to-text` | `alexgshaw/gcode-to-text:20251031` | `/app` | 900 | — |
| `git-leak-recovery` | `alexgshaw/git-leak-recovery:20251031` | `/app` | 900 | — |
| `git-multibranch` | `alexgshaw/git-multibranch:20251031` | `/app` | 900 | — |
| `gpt2-codegolf` | `alexgshaw/gpt2-codegolf:20251031` | `/app` | 900 | — |
| `headless-terminal` | `alexgshaw/headless-terminal:20251031` | `/app` | 900 | — |
| `hf-model-inference` | `alexgshaw/hf-model-inference:20251031` | `/app` | 900 | no PWD guard, no apt, no curl/wget |
| `install-windows-3.11` | `alexgshaw/install-windows-3.11:20251031` | `/app` | 3600 | — |
| `kv-store-grpc` | `alexgshaw/kv-store-grpc:20251031` | `/app` | 900 | no PWD guard, no apt, no curl/wget |
| `large-scale-text-editing` | `alexgshaw/large-scale-text-editing:20251031` | `/app` | 1200 | — |
| `largest-eigenval` | `alexgshaw/largest-eigenval:20251031` | `/app` | 900 | no apt, no curl/wget |
| `llm-inference-batching-scheduler` | `alexgshaw/llm-inference-batching-scheduler:20251031` | `/app` | 1800 | — |
| `log-summary-date-ranges` | `alexgshaw/log-summary-date-ranges:20251031` | `/app` | 900 | — |
| `mailman` | `alexgshaw/mailman:20251031` | `/app` | 1800 | — |
| `make-doom-for-mips` | `alexgshaw/make-doom-for-mips:20251031` | `/app` | 900 | — |
| `make-mips-interpreter` | `alexgshaw/make-mips-interpreter:20251031` | `/app` | 1800 | — |
| `mcmc-sampling-stan` | `alexgshaw/mcmc-sampling-stan:20251031` | `/app` → `/app` | 1800 | — |
| `merge-diff-arc-agi-task` | `alexgshaw/merge-diff-arc-agi-task:20251031` | `/app` | 900 | — |
| `model-extraction-relu-logits` | `alexgshaw/model-extraction-relu-logits:20251031` | `/app` | 900 | — |
| `modernize-scientific-stack` | `alexgshaw/modernize-scientific-stack:20251031` | `/app` | 600 | — |
| `mteb-leaderboard` | `alexgshaw/mteb-leaderboard:20251031` | `/app` | 3600 | — |
| `mteb-retrieve` | `alexgshaw/mteb-retrieve:20251031` | `/app` | 1800 | — |
| `multi-source-data-merger` | `alexgshaw/multi-source-data-merger:20251031` | `/app` | 900 | — |
| `nginx-request-logging` | `alexgshaw/nginx-request-logging:20251031` | `/app` | 900 | — |
| `openssl-selfsigned-cert` | `alexgshaw/openssl-selfsigned-cert:20251031` | `/app` | 900 | — |
| `overfull-hbox` | `alexgshaw/overfull-hbox:20251031` | `/app` | 360 | — |
| `password-recovery` | `alexgshaw/password-recovery:20251031` | `/app` | 900 | — |
| `path-tracing` | `alexgshaw/path-tracing:20251031` | `/app` | 1800 | — |
| `path-tracing-reverse` | `alexgshaw/path-tracing-reverse:20251031` | `/app` | 1800 | — |
| `polyglot-c-py` | `alexgshaw/polyglot-c-py:20251031` | `/app` | 900 | — |
| `polyglot-rust-c` | `alexgshaw/polyglot-rust-c:20251031` | `/app` | 900 | — |
| `portfolio-optimization` | `alexgshaw/portfolio-optimization:20251031` | `/app` | 3600 | — |
| `protein-assembly` | `alexgshaw/protein-assembly:20251031` | `/app` | 1800 | — |
| `prove-plus-comm` | `alexgshaw/prove-plus-comm:20251031` | `/workspace` | 900 | — |
| `pypi-server` | `alexgshaw/pypi-server:20251031` | `/app` | 900 | — |
| `pytorch-model-cli` | `alexgshaw/pytorch-model-cli:20251031` | `/app` | 900 | — |
| `pytorch-model-recovery` | `alexgshaw/pytorch-model-recovery:20251031` | `/app` | 900 | — |
| `qemu-alpine-ssh` | `alexgshaw/qemu-alpine-ssh:20251031` | `/app` | 900 | — |
| `qemu-startup` | `alexgshaw/qemu-startup:20251031` | `/app` | 900 | — |
| `query-optimize` | `alexgshaw/query-optimize:20251031` | `/app` | 1800 | — |
| `raman-fitting` | `alexgshaw/raman-fitting:20251031` | `/app` | 900 | — |
| `regex-chess` | `alexgshaw/regex-chess:20251031` | `/app` | 3600 | — |
| `regex-log` | `alexgshaw/regex-log:20251031` | `/app` | 900 | — |
| `reshard-c4-data` | `alexgshaw/reshard-c4-data:20251031` | `/app` | 3600 | no apt, no curl/wget |
| `rstan-to-pystan` | `alexgshaw/rstan-to-pystan:20251031` | `/app` → `/app` | 1800 | — |
| `sam-cell-seg` | `alexgshaw/sam-cell-seg:20251031` | `/app` | 7200 | — |
| `sanitize-git-repo` | `alexgshaw/sanitize-git-repo:20251031` | `/app` → `/app/dclm` | 900 | — |
| `schemelike-metacircular-eval` | `alexgshaw/schemelike-metacircular-eval:20251031` | `/app` | 2400 | — |
| `sparql-university` | `alexgshaw/sparql-university:20251031` | `/app` | 900 | — |
| `sqlite-db-truncate` | `alexgshaw/sqlite-db-truncate:20251031` | `/app` | 900 | — |
| `sqlite-with-gcov` | `alexgshaw/sqlite-with-gcov:20251031` | `/app` | 900 | — |
| `torch-pipeline-parallelism` | `alexgshaw/torch-pipeline-parallelism:20251031` | `/app` | 900 | — |
| `torch-tensor-parallelism` | `alexgshaw/torch-tensor-parallelism:20251031` | `/app` | 900 | — |
| `train-fasttext` | `alexgshaw/train-fasttext:20251031` | `/app` | 3600 | — |
| `tune-mjcf` | `alexgshaw/tune-mjcf:20251031` | `/app` | 900 | — |
| `video-processing` | `alexgshaw/video-processing:20251031` | `/app` | 3600 | — |
| `vulnerable-secret` | `alexgshaw/vulnerable-secret:20251031` | `/app` | 900 | — |
| `winning-avg-corewars` | `alexgshaw/winning-avg-corewars:20251031` | `/app` | 3600 | — |
| `write-compressor` | `alexgshaw/write-compressor:20251031` | `/app` | 900 | — |

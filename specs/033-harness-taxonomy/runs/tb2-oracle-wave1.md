# TB2 Oracle Admission Wave 1 Under V3 Egress None (2026-10-15)

Work 033 OD-15 wave 1 (D-134, D-135). Answers the admission-count half of interfaces §14 Q7. No model was called;
every run was a local container on the operator Mac. Tool: `scripts/tb2_oracle_wave.py` (screen, admit, report).

## Result

**Admitted: 0 of 89.** No task's own test entry (`bash /tests/test.sh`, `tb2_grading.entry_for`) starts its pytest
session under V3 egress none, so no reference solution can pass and nothing was written into the corpus
(`corpus/tb2/` and `corpus/bases/tb2.commits.json` stay absent; the corpus stays 2.1.0).

| Reason | Tasks | Meaning |
|---|---|---|
| `test_entry_offline` | 85 | the test runner never started offline (evidence: the last output lines, table below) |
| `not_pulled_size` | 4 | compressed image above 3 GB; not pulled (colima VM disk 20 GiB). Image config history checked instead (below) |

`test_entry_offline` by the last line of the screen output:

| Last output line (normalised) | Tasks | Why |
|---|---|---|
| `uvx: command not found` | 77 | `test.sh` downloads `uv` with `curl … astral.sh/uv/…/install.sh` (no network; 82 scripts also run `apt-get`, which needs root and network) |
| `pytest: command not found` | 3 | `pip install pytest==…` fails offline (`build-cython-ext`, `headless-terminal`, `kv-store-grpc`) |
| `failed to lookup address information` | 2 | `uvx` exists but cannot download Python 3.13 and the test wheels (`financial-document-processor`, `reshard-c4-data`) |
| `No module named pytest` | 1 | `largest-eigenval` |
| `uv: command not found` | 1 | `mailman` |
| `unrecognized arguments: --ctrf` | 1 | `fix-code-vulnerability`: pytest is in the image but `pytest-json-ctrf` cannot be installed offline, so pytest exits with a usage error before the session |

Every screen wrote reward `0` (85 of 85); none wrote `1`.

## Sources

- `harbor-framework/terminal-bench-2` at `2fd12b88aafdd04a52c298e3940bcb189f9766d6` (the `runs/tb2-q7.md` commit),
  Apache-2.0 (`tb2.check_license`), 89 tasks, `gpus > 0` none. Clone: operator scratch
  `~/.amplai-sandbox-probes/tb2-wave1/terminal-bench-2` (not committed).
- Images: `alexgshaw/<task>:20251031` from Docker Hub, each pinned by its registry digest after the pull (recorded
  per task in the operator scratch `screen/<task>.screen.json`). All 85 pulled images are `linux/amd64`; together
  19.06 GB uncompressed. Compressed sizes (registry manifests, all 89): 85 under 1.5 GB, 48.98 GB in total.

## Method

1. **Screen** (`tb2_oracle_wave.py screen`), one run per task: the task's own entry `bash /tests/test.sh` in the
   pinned task image, in the image's `WorkingDir` (`/app` for 86 tasks), with the containment of every V3 trial
   (`sandbox/container.py`): `--network none`, `--user 65534:65534`, `--read-only`, `--cap-drop=ALL`,
   `--security-opt=no-new-privileges`, tmpfs `/tmp` and `/home/agent`, `HOME=/home/agent`, memory 2 GB, 2 CPUs, 256
   pids; `tests/` read-only at `/tests`, a fresh `/logs/verifier`. The screen passes when pytest prints `test session
   starts`.
2. Why a failed screen is a failed admission: §10.5 step 4 (c) needs the reference to pass. The reference step runs
   the same script in the same image under the same containment; the solution changes only `/workspace`, while the
   runner bootstrap (`curl` of `uv`, `uvx`/`pip` installs, `apt-get`) needs network or a writable root outside it.
   Every TB2 `test.sh` uses pytest (89 of 89).
3. **Control** (full admission, `tb2_oracle_wave.py admit`, repeats 1, scratch only): `chess-best-move` went through
   extract, base commit, the oracle image (`FROM` the pinned task image, `/app` linked to `/workspace`, uid 65534, no
   driver layer), push to `localhost:5000`, and the three admission steps. Result `solution_failed`: the unmodified
   tests reward `0`, the solution exits 1 (its `apt install stockfish` and `pip3 install` need network), the
   reference tests reward `0`. The full path agrees with the screen.
4. The 4 large images were not pulled. Their image config (registry blob, no layers) shows no `uv` and no `pytest`
   install in the build history: `mteb-retrieve`, `mteb-leaderboard` and `pytorch-model-recovery` run `curl` for
   `uv` then `uvx` in `test.sh`; `hf-model-inference` runs `pip install pytest==8.4.1 …` then `pytest`. That they
   would also fail the screen is (추정) from the history; it was not run.

## Findings Beyond The Count

- **Driver layer architecture (§14 Q7, "whether the driver layer runs on each task base image")**: every TB2 image
  is `linux/amd64`; the local worker images (`localhost:5000/amplai-worker:local`, `…-amplai-bench-app:local`) are
  `linux/arm64`. `tb2.image_spec` copies the driver paths from the worker base image, so on this host the driver
  layer would be arm64 binaries in an amd64 image. An amd64 worker build is needed before any TB2 trial (확인 필요:
  whether the driver CLIs run under emulation at usable speed). TB2 images ran here under the colima VM's binfmt
  emulation.
- **Empty working directories**: `cancel-async-tasks` and `build-cython-ext` have an empty `/app`;
  `tb2.commit_base` refuses an empty tree (`TB2_SPEC: the extracted working directory has no files`), so such a task
  cannot get a base commit today. How many of the 89 have an empty working directory is 확인 필요 (seen on these 2).
- **Docker Hub pull limit**: unauthenticated pulls are limited (`ratelimit-limit: 100;w=3600` read from the registry
  headers). The first pass hit it for 15 tasks (`toomanyrequests` on pull; that the 89 manifest reads counted toward
  it is (추정)); those 15 were re-screened with `--again` and are in the table.

## What Would Admit TB2 Tasks (Operator Decision, Not Implemented)

Under egress none, as decided, TB2 adds no task. The ways forward change the grading environment or the decision
and are the operator's:

- (A) Bake each task's test dependencies into the oracle/trial image at build time (build with network, run
  without): `uv`, Python 3.13 and the pinned test wheels (`pytest==8.4.1`, `pytest-json-ctrf==0.3.5`, task extras)
  in a read-only location, and a test entry that runs them without `curl`/`apt-get`. This changes `test.sh` or its
  environment (a grading contract change, §10.5 step 6) and puts the test tools inside the agent's image. Whether
  `uvx` runs from a read-only cache as uid 65534 is 확인 필요. Separately 25 `solve.sh` run `apt`, 12 `pip install`,
  7 `git clone`, 7 `curl`/`wget` (static count), so those references would also fail offline.
- (B) Allow network for the grading container only (the agent stays offline). Changes the egress-none rule of D-135
  for grading.
- (C) Leave TB2 out and fill IC-20 from SWE-bench Multilingual (next in the D-135 order).

## Per Task

`stage` is where the task stopped; `evidence` is the last two lines of the screen output (operator scratch
`screen/<task>.screen.log` keeps the whole output).

| Task | Category | Stage | Result | Reason | Evidence |
|---|---|---|---|---|---|
| `adaptive-rejection-sampler` | scientific-computing | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `bn-fit-modify` | scientific-computing | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `break-filter-js-from-html` | security | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `build-cython-ext` | debugging | screen | failed | `test_entry_offline` | ERROR: No matching distribution found for pytest==8.4.1; /tests/test.sh: line 11: pytest: command not found |
| `build-pmars` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `build-pov-ray` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 9: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 17: uvx: command not found |
| `caffe-cifar-10` | machine-learning | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `cancel-async-tasks` | software-engineering | screen | failed | `test_entry_offline` | cp: cannot create regular file '/app/test.py': Read-only file system; /tests/test.sh: line 20: uvx: command not found |
| `chess-best-move` | games | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `circuit-fibsqrt` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `cobol-modernization` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `code-from-image` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `compile-compcert` | system-administration | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `configure-git-webserver` | system-administration | screen | failed | `test_entry_offline` | /tests/test.sh: line 6: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 15: uvx: command not found |
| `constraints-scheduling` | personal-assistant | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `count-dataset-tokens` | model-training | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `crack-7z-hash` | security | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `custom-memory-heap-crash` | debugging | screen | failed | `test_entry_offline` | /tests/test.sh: line 12: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 21: uvx: command not found |
| `db-wal-recovery` | file-operations | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `distribution-search` | machine-learning | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `dna-assembly` | scientific-computing | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `dna-insert` | scientific-computing | screen | failed | `test_entry_offline` | /tests/test.sh: line 9: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 18: uvx: command not found |
| `extract-elf` | file-operations | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `extract-moves-from-video` | file-operations | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `feal-differential-cryptanalysis` | mathematics | screen | failed | `test_entry_offline` | /tests/test.sh: line 9: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 18: uvx: command not found |
| `feal-linear-cryptanalysis` | mathematics | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `filter-js-from-html` | security | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `financial-document-processor` | data-processing | screen | failed | `test_entry_offline` | Caused by: dns error; Caused by: failed to lookup address information: Try again |
| `fix-code-vulnerability` | security | screen | failed | `test_entry_offline` | inifile: None; rootdir: /tests |
| `fix-git` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `fix-ocaml-gc` | software-engineering | screen | failed | `test_entry_offline` | make: *** No rule to make target '../Makefile.build_config'.  Stop.; /tests/test.sh: line 31: uvx: command not found |
| `gcode-to-text` | file-operations | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `git-leak-recovery` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `git-multibranch` | system-administration | screen | failed | `test_entry_offline` | /tests/test.sh: line 9: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 18: uvx: command not found |
| `gpt2-codegolf` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `headless-terminal` | software-engineering | screen | failed | `test_entry_offline` | ERROR: No matching distribution found for pytest==8.4.1; /tests/test.sh: line 14: pytest: command not found |
| `hf-model-inference` | data-science | screen | failed | `not_pulled_size` | 6.19 GB compressed |
| `install-windows-3.11` | system-administration | screen | failed | `test_entry_offline` | E: List directory /var/lib/apt/lists/partial is missing. - Acquire (30: Read-only file system); /tests/test.sh: line 22: uvx: command not found |
| `kv-store-grpc` | software-engineering | screen | failed | `test_entry_offline` | ERROR: No matching distribution found for pytest==8.4.2; /tests/test.sh: line 6: pytest: command not found |
| `large-scale-text-editing` | file-operations | screen | failed | `test_entry_offline` | OSError: [Errno 30] Read-only file system: 'input.csv'; /tests/test.sh: line 28: uvx: command not found |
| `largest-eigenval` | mathematics | screen | failed | `test_entry_offline` | ERROR: No matching distribution found for pytest==8.4.1; /usr/local/bin/python: No module named pytest |
| `llm-inference-batching-scheduler` | machine-learning | screen | failed | `test_entry_offline` | /tests/test.sh: line 12: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 21: uvx: command not found |
| `log-summary-date-ranges` | data-processing | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `mailman` | system-administration | screen | failed | `test_entry_offline` | /tests/test.sh: line 26: uv: command not found; /tests/test.sh: line 28: uv: command not found |
| `make-doom-for-mips` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `make-mips-interpreter` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `mcmc-sampling-stan` | data-science | screen | failed | `test_entry_offline` | /tests/test.sh: line 12: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 23: uvx: command not found |
| `merge-diff-arc-agi-task` | debugging | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `model-extraction-relu-logits` | mathematics | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `modernize-scientific-stack` | scientific-computing | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `mteb-leaderboard` | data-science | screen | failed | `not_pulled_size` | 8.82 GB compressed |
| `mteb-retrieve` | data-science | screen | failed | `not_pulled_size` | 8.81 GB compressed |
| `multi-source-data-merger` | data-processing | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `nginx-request-logging` | system-administration | screen | failed | `test_entry_offline` | /tests/test.sh: line 9: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 18: uvx: command not found |
| `openssl-selfsigned-cert` | security | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `overfull-hbox` | debugging | screen | failed | `test_entry_offline` | /tests/test.sh: line 15: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 24: uvx: command not found |
| `password-recovery` | security | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `path-tracing` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `path-tracing-reverse` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `polyglot-c-py` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `polyglot-rust-c` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `portfolio-optimization` | optimization | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `protein-assembly` | scientific-computing | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `prove-plus-comm` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `pypi-server` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `pytorch-model-cli` | model-training | screen | failed | `test_entry_offline` | /tests/test.sh: line 9: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 18: uvx: command not found |
| `pytorch-model-recovery` | model-training | screen | failed | `not_pulled_size` | 6.10 GB compressed |
| `qemu-alpine-ssh` | system-administration | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `qemu-startup` | system-administration | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `query-optimize` | data-science | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `raman-fitting` | scientific-computing | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `regex-chess` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `regex-log` | data-processing | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `reshard-c4-data` | data-science | screen | failed | `test_entry_offline` | Caused by: dns error; Caused by: failed to lookup address information: Try again |
| `rstan-to-pystan` | data-science | screen | failed | `test_entry_offline` | /tests/test.sh: line 9: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `sam-cell-seg` | data-science | screen | failed | `test_entry_offline` | cp: cannot create regular file './correct_output.csv': Read-only file system; /tests/test.sh: line 30: uvx: command not found |
| `sanitize-git-repo` | security | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `schemelike-metacircular-eval` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `sparql-university` | data-querying | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `sqlite-db-truncate` | debugging | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `sqlite-with-gcov` | system-administration | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `torch-pipeline-parallelism` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `torch-tensor-parallelism` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `train-fasttext` | model-training | screen | failed | `test_entry_offline` | tar: Exiting with failure status due to previous errors; /tests/test.sh: line 21: uvx: command not found |
| `tune-mjcf` | scientific-computing | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `video-processing` | video-processing | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `vulnerable-secret` | security | screen | failed | `test_entry_offline` | /tests/test.sh: line 13: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 22: uvx: command not found |
| `winning-avg-corewars` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |
| `write-compressor` | software-engineering | screen | failed | `test_entry_offline` | /tests/test.sh: line 10: /home/agent/.local/bin/env: No such file or directory; /tests/test.sh: line 19: uvx: command not found |

## Reproduce

```bash
W=~/.amplai-sandbox-probes/tb2-wave1
git clone https://github.com/harbor-framework/terminal-bench-2.git $W/terminal-bench-2
git -C $W/terminal-bench-2 checkout 2fd12b88aafdd04a52c298e3940bcb189f9766d6
.venv/bin/python scripts/tb2_oracle_wave.py screen --source $W/terminal-bench-2 \
    --commit 2fd12b88aafdd04a52c298e3940bcb189f9766d6 --out $W/screen
.venv/bin/python scripts/tb2_oracle_wave.py admit --source $W/terminal-bench-2 \
    --commit 2fd12b88aafdd04a52c298e3940bcb189f9766d6 --out $W/screen --base-repo $W/amplai-tb2 \
    --commits-file specs/033-harness-taxonomy/corpus/bases/tb2.commits.json \
    --dest specs/033-harness-taxonomy/corpus/tb2     # admits only screened tasks; none this wave
.venv/bin/python scripts/tb2_oracle_wave.py report --out $W/screen
```

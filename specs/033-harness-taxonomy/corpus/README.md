# Corpus v2 (`amplai-bench-v2`)

Work 033 corpus (D-098). Normative layout, `task.json` v2, split rule, authoring rules and leak
gate: `specs/033-harness-taxonomy/interfaces.md` §10. Code: `meta_harness/corpus_v2.py`,
`meta_harness/leak_gate.py`.

## Layout

```text
manifest.json            corpus_id, version, split_seed, bases (no task list)
splits.json              seed, method, assignments (written by S6-freeze; absent until then)
bases/demo/              Work 030 base tree (imported, byte-identical to its base/)
bases/bench/, bench.commit   richer base app (S6-base)
bases/tb2.commits.json   per-task TB2 base commits (TB2 adapter, S7a/S16)
tasks/<id>/              task.json, hidden/, reference/ (+ reference_alt/, hidden_alt/ for
                         ambiguity tasks that expect a question)
tb2/<name>/              TB2 adapter output (S7a)
```

`corpus_v2.load` scans `tasks/*/task.json` and `tb2/*/task.json`. Authors never set `split`
(`TASK_SPLIT` otherwise); `assign_splits` computes it and S6-freeze writes `splits.json`.

## Current Content

- `tasks/work030-*`: the 20 Work 030 tasks imported by `corpus_v2.import_work030` as the
  regression set (`set: regression`, `domain: regression`, `base: demo`). They freeze as their
  own corpus `amplai-regression-v1` with every case in `validation` (§10.6). Their
  `hidden_map` is empty: Work 030 had no map and the full-map rule (§10.3 rule 2) binds own tasks.
- `manifest.json`: `split_seed` is `null` until S6-freeze sets it. The `bench` base has no
  `app_id` yet (확인 필요: not named in §10.1; S6-base/S6-freeze fill it).

## Commands

```bash
.venv/bin/python scripts/corpus_check.py --corpus specs/033-harness-taxonomy/corpus --repeats 3
.venv/bin/python scripts/corpus_check.py --corpus specs/033-harness-taxonomy/corpus --domain bug
```

A task is fair when its hidden tests fail on the base and pass on the reference, the visible
tests pass on both, and the verdict is the same in every repeat. An ambiguity task that expects
a question also needs the proof: each reference passes its own hidden suite and fails the other's.
`tb2_tests` tasks are listed as SKIP; their fairness is the TB2 admission run (§10.5).

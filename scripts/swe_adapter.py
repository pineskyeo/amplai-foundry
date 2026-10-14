"""SWE-bench Multilingual and SWE-Bench Pro adapter skeleton (Work 033 OD-15 wave 1, D-135).

Contract: ``specs/033-harness-taxonomy/interfaces.md`` "Clarification: SWE-bench Adapter Skeleton
(2026-10-15)". Import order (D-135): Terminal-Bench 2.0, then SWE-bench Multilingual (unsaturated
tasks), then SWE-Bench Pro V2 HARD-51. This module reads dataset rows the operator exported to a
local JSONL file; it downloads nothing, runs no container and writes nothing into the corpus.

Source facts (checked 2026-10-15 against the Hugging Face datasets-server ``info`` endpoint and
the GitHub API):

- SWE-bench Multilingual: ``SWE-bench/SWE-bench_Multilingual`` (card ``license: mit``), split
  ``test``, 300 rows. Columns ``instance_id, repo, base_commit, patch, test_patch,
  problem_statement, hints_text, created_at, version, FAIL_TO_PASS, PASS_TO_PASS, image,
  log_parser, eval_script, eval_type``; ``FAIL_TO_PASS``/``PASS_TO_PASS`` are lists of strings.
  Harness ``SWE-bench/SWE-bench`` (MIT). One image per instance (``image`` column, x86_64).
  Per-instance results of 14 submissions: ``SWE-bench/experiments``
  ``evaluation/multilingual/*/per_instance_details.json`` (``{id: {"resolved": bool, ...}}``).
- SWE-Bench Pro: ``ScaleAI/SWE-bench_Pro`` configs ``default`` (V2, 642 rows), ``hard``
  (HARD-51, 51 rows) and ``v1`` (731 rows); the card has no ``license:`` field. Columns ``repo,
  instance_id, base_commit, patch, test_patch, problem_statement, requirements, interface,
  repo_language, fail_to_pass, pass_to_pass, issue_specificity, issue_categories,
  before_repo_set_cmd, selected_test_files_to_run, dockerhub_tag, docker_image, hard, version``;
  ``fail_to_pass``/``pass_to_pass`` are JSON-encoded strings. Harness
  ``scaleapi/SWE-bench_Pro-os`` (MIT), ``v2/hard51_ids.txt`` (51 ids): "The 51 tasks failed by at
  least two of five model families ... minus three tasks later found ambiguous" (``v2/README.md``).
  Images ``ghcr.io/scaleapi/swe-bench_pro-v2:<instance_id>`` (linux/amd64). The paper states the
  public set uses "repositories with GPL and other copyleft licenses" (arXiv 2509.16941).

What stays open (확인 필요, not defaulted here):

- The license of each upstream repository at the task's ``base_commit``: the caller passes
  ``repo_licenses`` explicitly, keyed by ``(repo, base_commit)``; an instance without an entry
  for its own base commit holds ``SWE_LICENSE``. Only the
  permissive identifiers in ``PERMISSIVE`` pass; copyleft or source-available licenses (all
  SWE-Bench Pro repositories at HEAD) hold ``SWE_LICENSE`` until the operator decides how such
  task content may be stored.
- The saturation threshold: ``select_unsaturated`` takes ``max_resolved_share`` as a required
  argument.
- The corpus contract: ``corpus_v2`` has no SWE source kind, grading or per-instance base
  (``SOURCE_KINDS``, ``GRADINGS``, ``LICENSES``); adding them is an operator decision.
- Running the per-instance images under V3 containment (network none, uid 65534, read-only
  root): no image was pulled.

It lives in ``scripts/``, not ``meta_harness/``: no trial imports it, and a new package file
would change the D-128 ``trial_code_digest`` (every earlier trial non-reusable). It moves into
the package with the corpus contract change that adds a SWE source kind.

The command line reads a local JSONL export of a dataset configuration and prints JSON:

    .venv/bin/python scripts/swe_adapter.py scan --source multilingual --rows ROWS.jsonl
    .venv/bin/python scripts/swe_adapter.py licenses --source pro_hard --rows ROWS.jsonl \
        --repo-licenses LICENSES.json          # {"owner/name": {"<base_commit>": "SPDX"}}
    .venv/bin/python scripts/swe_adapter.py select --source multilingual --rows ROWS.jsonl \
        --results per_instance_details.json [--results ...] --max-resolved-share 0.2
    .venv/bin/python scripts/swe_adapter.py hard --source pro_v2 --rows ROWS.jsonl \
        --ids hard51_ids.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from amplai_foundry.runtime.errors import Hold, RuntimeFault

TestEncoding = Literal["list", "json_string"]


@dataclass(frozen=True)
class SweSource:
    """One dataset configuration and how its rows are read."""

    key: str
    dataset: str
    config: str
    split: str
    rows: int  # the published row count of the configuration
    harness: str
    harness_license: str
    dataset_license: str | None  # the card's ``license:`` field; None when the card has none
    tests_encoding: TestEncoding
    fail_to_pass: str  # column names
    pass_to_pass: str
    image: str
    language: str | None


SOURCES: dict[str, SweSource] = {
    "multilingual": SweSource(
        "multilingual",
        "SWE-bench/SWE-bench_Multilingual",
        "default",
        "test",
        300,
        "SWE-bench/SWE-bench",
        "MIT",
        "MIT",
        "list",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "image",
        None,
    ),
    "pro_v2": SweSource(
        "pro_v2",
        "ScaleAI/SWE-bench_Pro",
        "default",
        "test",
        642,
        "scaleapi/SWE-bench_Pro-os",
        "MIT",
        None,
        "json_string",
        "fail_to_pass",
        "pass_to_pass",
        "docker_image",
        "repo_language",
    ),
    "pro_hard": SweSource(
        "pro_hard",
        "ScaleAI/SWE-bench_Pro",
        "hard",
        "test",
        51,
        "scaleapi/SWE-bench_Pro-os",
        "MIT",
        None,
        "json_string",
        "fail_to_pass",
        "pass_to_pass",
        "docker_image",
        "repo_language",
    ),
}
COMMON_FIELDS = ("instance_id", "repo", "base_commit", "patch", "test_patch", "problem_statement")
# SPDX identifiers whose content the corpus may hold without an operator decision. Copyleft
# (GPL, AGPL, LGPL) and source-available licenses (BUSL, SSPL, RSAL, FCL) are not listed.
PERMISSIVE = ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Unlicense")
COMMIT = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class SweInstance:
    source: str
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    patch: str
    test_patch: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    image: str | None
    language: str | None
    extra: dict[str, Any] = field(default_factory=dict)  # every other column, unread


def _source_error(message: str) -> Hold:
    return Hold("SWE_SOURCE", message)


def source(key: str) -> SweSource:
    try:
        return SOURCES[key]
    except KeyError:
        raise RuntimeFault("SWE_SOURCE", f"unknown SWE source {key!r}") from None


def _tests(value: Any, encoding: TestEncoding, where: str) -> tuple[str, ...]:
    if encoding == "json_string":
        if not isinstance(value, str):
            raise _source_error(f"{where}: a JSON-encoded list of test ids is required")
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise _source_error(f"{where}: not valid JSON") from exc
    if not isinstance(value, list) or not all(isinstance(t, str) and t for t in value):
        raise _source_error(f"{where}: a list of test ids is required")
    return tuple(value)


def parse_row(key: str, row: Mapping[str, Any]) -> SweInstance:
    """One dataset row of source ``key`` (``SOURCES``); ``SWE_SOURCE`` when malformed.

    A task needs at least one FAIL_TO_PASS test (the hidden tests that must flip).
    """
    src = source(key)
    if not isinstance(row, Mapping):
        raise _source_error(f"{key}: a row is a JSON object")
    name = row.get("instance_id")
    where = f"{key}:{name!r}"
    missing = [f for f in COMMON_FIELDS if not isinstance(row.get(f), str) or not row.get(f)]
    if missing:
        raise _source_error(f"{where}: missing or empty {missing}")
    if not REPO.fullmatch(row["repo"]):
        raise _source_error(f"{where}: repo is owner/name")
    if not COMMIT.fullmatch(row["base_commit"]):
        raise _source_error(f"{where}: base_commit is a 40-hex sha")
    fail = _tests(row.get(src.fail_to_pass), src.tests_encoding, f"{where} {src.fail_to_pass}")
    keep = _tests(row.get(src.pass_to_pass), src.tests_encoding, f"{where} {src.pass_to_pass}")
    if not fail:
        raise _source_error(f"{where}: no {src.fail_to_pass} test")
    image = row.get(src.image)
    language = row.get(src.language) if src.language else None
    known = {*COMMON_FIELDS, src.fail_to_pass, src.pass_to_pass, src.image}
    if src.language:
        known.add(src.language)
    return SweInstance(
        key,
        row["instance_id"],
        row["repo"],
        row["base_commit"],
        row["problem_statement"],
        row["patch"],
        row["test_patch"],
        fail,
        keep,
        image if isinstance(image, str) and image else None,
        language if isinstance(language, str) and language else None,
        {k: v for k, v in sorted(row.items()) if k not in known},
    )


def read_rows(key: str, path: Path) -> list[SweInstance]:
    """Every row of a local JSONL export of source ``key``; duplicate ids hold ``SWE_SOURCE``."""
    instances: list[SweInstance] = []
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise _source_error(f"{path}: unreadable") from exc
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise _source_error(f"{path}:{number}: not a JSON line") from exc
        instances.append(parse_row(key, row))
    ids = [i.instance_id for i in instances]
    if len(set(ids)) != len(ids):
        raise _source_error(f"{path}: duplicate instance_id")
    return instances


LicenseDecisions = Mapping[tuple[str, str], str]  # {(repo, base_commit): SPDX}


def read_license_decisions(value: Any) -> dict[tuple[str, str], str]:
    """``{(repo, base_commit): SPDX}`` from the operator's JSON ``{repo: {base_commit: SPDX}}``
    (``--repo-licenses``). A repository's license can differ between base commits, so each
    decision names both; anything else holds ``SWE_LICENSE``."""
    shape = Hold("SWE_LICENSE", "license decisions are {repo: {40-hex base_commit: SPDX}}")
    if not isinstance(value, dict):
        raise shape
    decisions: dict[tuple[str, str], str] = {}
    for repo, commits in value.items():
        if not isinstance(repo, str) or not isinstance(commits, dict):
            raise shape
        for commit, spdx in commits.items():
            if not COMMIT.fullmatch(str(commit)) or not isinstance(spdx, str) or not spdx:
                raise shape
            decisions[(repo, commit)] = spdx
    return decisions


def license_gate(
    instances: Sequence[SweInstance], repo_licenses: LicenseDecisions
) -> tuple[list[SweInstance], list[dict[str, str]]]:
    """``(passed, held)``: an instance passes when the license of its repository at its base
    commit (``repo_licenses[(repo, base_commit)]``, supplied by the operator) is in
    ``PERMISSIVE``.

    An instance without an entry for its ``(repo, base_commit)`` is held with reason
    ``license_unknown`` (a decision for another commit of the repository does not carry over);
    any other license with ``license_not_permissive``. Nothing is inferred from the dataset card
    or the repository HEAD.
    """
    passed: list[SweInstance] = []
    held: list[dict[str, str]] = []
    for instance in instances:
        spdx = repo_licenses.get((instance.repo, instance.base_commit))
        if spdx is None:
            held.append({"instance_id": instance.instance_id, "reason": "license_unknown"})
        elif spdx not in PERMISSIVE:
            held.append(
                {
                    "instance_id": instance.instance_id,
                    "reason": "license_not_permissive",
                    "license": spdx,
                }
            )
        else:
            passed.append(instance)
    return passed, held


def require_licenses(instances: Sequence[SweInstance], repo_licenses: LicenseDecisions) -> None:
    """``Hold SWE_LICENSE`` unless every instance passes ``license_gate``."""
    _, held = license_gate(instances, repo_licenses)
    if held:
        raise Hold(
            "SWE_LICENSE", f"{len(held)} instance(s) without a permissive license", details=held
        )


def resolution_counts(details: Iterable[Mapping[str, Any]]) -> dict[str, tuple[int, int]]:
    """``{instance_id: (resolved, submissions)}`` over ``per_instance_details.json`` objects of
    ``SWE-bench/experiments`` (``{id: {"resolved": bool, ...}}``). An entry without a boolean
    ``resolved`` holds ``SWE_RESULTS``."""
    counts: dict[str, list[int]] = {}
    for number, submission in enumerate(details):
        if not isinstance(submission, Mapping):
            raise Hold("SWE_RESULTS", f"submission {number}: not an object")
        for instance_id, entry in submission.items():
            resolved = entry.get("resolved") if isinstance(entry, Mapping) else None
            if not isinstance(resolved, bool):
                raise Hold("SWE_RESULTS", f"submission {number}: {instance_id}: resolved is a bool")
            pair = counts.setdefault(str(instance_id), [0, 0])
            pair[0] += int(resolved)
            pair[1] += 1
    return {k: (v[0], v[1]) for k, v in sorted(counts.items())}


def select_unsaturated(
    instances: Sequence[SweInstance],
    counts: Mapping[str, tuple[int, int]],
    *,
    max_resolved_share: float,
    min_submissions: int = 1,
) -> tuple[list[SweInstance], list[dict[str, Any]]]:
    """``(selected, dropped)``: an instance is selected when at least ``min_submissions``
    submissions ran it and at most ``max_resolved_share`` of them resolved it. The threshold is
    the operator's (no default); instances without results are dropped as ``no_results``."""
    if not 0.0 <= max_resolved_share <= 1.0 or min_submissions < 1:
        raise RuntimeFault("SWE_SELECT", "0 <= max_resolved_share <= 1 and min_submissions >= 1")
    selected: list[SweInstance] = []
    dropped: list[dict[str, Any]] = []
    for instance in sorted(instances, key=lambda i: i.instance_id):
        resolved, total = counts.get(instance.instance_id, (0, 0))
        if total < min_submissions:
            dropped.append({"instance_id": instance.instance_id, "reason": "no_results"})
        elif resolved / total > max_resolved_share:
            dropped.append(
                {
                    "instance_id": instance.instance_id,
                    "reason": "saturated",
                    "resolved": resolved,
                    "submissions": total,
                }
            )
        else:
            selected.append(instance)
    return selected, dropped


def hard_subset(instances: Sequence[SweInstance], ids: Iterable[str]) -> list[SweInstance]:
    """The HARD-51 instances of a Pro V2 export by ``v2/hard51_ids.txt`` (one id per line).

    Every listed id must be present, and when the rows carry the ``hard`` column it must be true
    exactly for the listed ids (``SWE_SOURCE`` otherwise)."""
    wanted = [i.strip() for i in ids if i.strip()]
    if len(set(wanted)) != len(wanted):
        raise _source_error("hard id list: duplicate id")
    by_id = {i.instance_id: i for i in instances}
    missing = [i for i in wanted if i not in by_id]
    if missing:
        raise _source_error(f"hard id list: {len(missing)} id(s) not in the rows: {missing[:3]}")
    flagged = {i.instance_id for i in instances if i.extra.get("hard") is True}
    if any("hard" in i.extra for i in instances) and flagged != set(wanted):
        raise _source_error("the rows' hard column disagrees with the hard id list")
    return [by_id[i] for i in sorted(wanted)]


# -- command line -------------------------------------------------------------------------------


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeFault("SWE_SOURCE", f"{path}: unreadable JSON") from exc


def _scan(args: argparse.Namespace) -> int:
    rows = read_rows(args.source, args.rows)
    src = source(args.source)
    _print(
        {
            "source": args.source,
            "dataset": f"{src.dataset}:{src.config}/{src.split}",
            "rows": len(rows),
            "published_rows": src.rows,
            "repos": dict(sorted(Counter(r.repo for r in rows).items())),
            "languages": dict(sorted(Counter(r.language or "unknown" for r in rows).items())),
        }
    )
    return 0


def _licenses(args: argparse.Namespace) -> int:
    rows = read_rows(args.source, args.rows)
    passed, held = license_gate(rows, read_license_decisions(_json(args.repo_licenses)))
    _print({"passed": [i.instance_id for i in passed], "held": held})
    return 0 if not held else 1


def _select(args: argparse.Namespace) -> int:
    rows = read_rows(args.source, args.rows)
    counts = resolution_counts(_json(path) for path in args.results)
    selected, dropped = select_unsaturated(
        rows,
        counts,
        max_resolved_share=args.max_resolved_share,
        min_submissions=args.min_submissions,
    )
    _print({"selected": [i.instance_id for i in selected], "dropped": dropped})
    return 0


def _hard(args: argparse.Namespace) -> int:
    rows = read_rows(args.source, args.rows)
    picked = hard_subset(rows, args.ids.read_text().splitlines())
    _print({"hard": [i.instance_id for i in picked], "count": len(picked)})
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SWE-bench adapter skeleton (Work 033 OD-15).")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "licenses", "select", "hard"):
        p = sub.add_parser(name)
        p.add_argument("--source", required=True, choices=sorted(SOURCES))
        p.add_argument("--rows", type=Path, required=True, help="a local JSONL export")
    sub.choices["licenses"].add_argument("--repo-licenses", type=Path, required=True)
    select = sub.choices["select"]
    select.add_argument("--results", type=Path, action="append", required=True)
    select.add_argument("--max-resolved-share", type=float, required=True)
    select.add_argument("--min-submissions", type=int, default=1)
    sub.choices["hard"].add_argument("--ids", type=Path, required=True)
    return parser


COMMANDS = {"scan": _scan, "licenses": _licenses, "select": _select, "hard": _hard}


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except RuntimeFault as exc:
        print(f"swe_adapter: {exc.code}: {exc.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

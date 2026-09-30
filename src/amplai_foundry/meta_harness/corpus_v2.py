"""Corpus v2: domain-tagged tasks, seeded splits, graders and the frozen task index (D-098).

Layout under ``specs/033-harness-taxonomy/corpus/`` (interfaces §10.1)::

    manifest.json                corpus_id, version, split_seed, bases (no task list)
    splits.json                  {"seed", "method", "assignments": {task: split}} (S6-freeze)
    bases/<base>/                base app trees
    tasks/<id>/task.json         task.json v2 (§10.2); hidden/, reference/ as Work 030
    tasks/<id>/reference_alt/, hidden_alt/    ambiguity tasks with ``expected: ask`` only
    tb2/<name>/task.json         TB2 adapter output: tests/, solution/ (S7a)

``load`` scans the task directories; there is no task list. Authors never set ``split``:
``assign_splits`` computes it (``stratified_by_domain_v1``) and S6-freeze writes ``splits.json``.
Regression-set tasks are all ``validation`` and are frozen as their own corpus (§10.6).
"""

from __future__ import annotations

import difflib
import json
import random
import re
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Literal

from ..evaluation.corpus import CorpusService
from ..runtime.contracts.authority import Actor
from ..runtime.contracts.identity import ID, canonical, digest
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.evidence.cas import ArtifactStore
from ..runtime.storage.store import Store
from . import local_corpus
from .local_corpus import HIDDEN_DIR, CorpusError, Outcome

Ref = dict[str, Any]

DOMAINS = ("bug", "feature", "refactor", "cli_ops", "data", "ambiguity", "terminal", "regression")
SETS = ("main", "regression")
SOURCE_KINDS = ("own", "tb2", "work030")
LICENSES = ("Apache-2.0", "LicenseRef-amplai-internal")
GRADINGS = ("pytest_hidden", "tb2_tests", "planner_questions")
SPLITS = ("development", "validation", "holdout")
SPLIT_METHOD = "stratified_by_domain_v1"
# §10.6: the regression set is its own corpus with every case in validation. The method label of
# its task index is not named by the spec (reported as open).
REGRESSION_CORPUS_ID = "amplai-regression-v1"
REGRESSION_METHOD = "regression_all_validation_v1"
# §10.2 minimums: IC-09 needs 16 tasks per confirmatory stage; only informative validation tasks
# are used, so validation needs more.
HOLDOUT_MIN = 16
VALIDATION_MIN = 24
TASK_KEYS = frozenset(
    {
        "task_id",
        "version",
        "domain",
        "subdomain",
        "set",
        "split",
        "source",
        "license",
        "base",
        "environment",
        "grading",
        "objective",
        "acceptance",
        "hidden_map",
        "ambiguity",
        "difficulty_declared",
    }
)
AMBIGUITY_KEYS = frozenset({"expected", "must_mention_any", "interpretations"})
TEST_FUNCTION = re.compile(rb"^[ \t]*(?:async[ \t]+)?def[ \t]+(test\w*)[ \t]*\(", re.MULTILINE)
SHA1 = re.compile(r"[0-9a-f]{40}")
# Work 030 import (§10.6): the 20 tasks came with commit af92975 (2026-09-30, Work 030 S4).
WORK030_AUTHOR = "Work 030 S4 (af92975)"
WORK030_CREATED = "2026-09-30"


@dataclass(frozen=True)
class TaskV2:
    task_id: str
    domain: str
    split: str | None
    set: Literal["main", "regression"]
    source: dict[str, str]
    license: str
    base_id: str
    environment_id: str
    grading: Literal["pytest_hidden", "tb2_tests", "planner_questions"]
    objective: str
    acceptance: tuple[str, ...]
    hidden: dict[str, bytes]
    reference: dict[str, bytes]
    reference_alt: dict[str, bytes]
    hidden_map: dict[str, int]
    ambiguity: dict[str, Any] | None
    tb2: dict[str, Any] | None
    # Not in the §3.11 dataclass, which omits the ``hidden_alt/`` tests of §10.1/§10.3; appended
    # with a default so positional construction by the §3.11 field list still works.
    hidden_alt: dict[str, bytes] = field(default_factory=dict)

    def contract_text(self) -> str:
        """Exactly ``CorpusTask.contract_text`` (``local_corpus.py:50-54``)."""
        lines = [self.objective.strip(), "", "Acceptance:"]
        lines += [f"- {statement}" for statement in self.acceptance]
        return "\n".join(lines)


@dataclass(frozen=True)
class CorpusV2:
    corpus_id: str
    version: str
    root: Path
    bases: dict[str, dict[str, str]]
    tasks: tuple[TaskV2, ...]
    # The manifest's ``split_seed`` (§10.1); appended with a default, not in the §3.11 field list.
    split_seed: int | None = None

    def task(self, task_id: str) -> TaskV2:
        for item in self.tasks:
            if item.task_id == task_id:
                return item
        raise CorpusError("TASK_UNKNOWN", f"No task {task_id}")


# -- loading -------------------------------------------------------------------------------------


def _json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise CorpusError(code, f"{path}: unreadable JSON") from exc


def _files(root: Path, prefix: Path = Path()) -> dict[str, bytes]:
    return local_corpus._files(root, prefix)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _manifest(root: Path) -> dict[str, Any]:
    manifest = _json(root / "manifest.json", "TASK_META")
    if not isinstance(manifest, dict) or not all(
        _text(manifest.get(k)) for k in ("corpus_id", "version")
    ):
        raise CorpusError("TASK_META", "manifest.json needs corpus_id and version")
    seed = manifest.get("split_seed")
    if seed is not None and (type(seed) is not int):
        raise CorpusError("TASK_SPLIT", "manifest split_seed must be an integer or null")
    bases = manifest.get("bases")
    if (
        not isinstance(bases, dict)
        or not bases
        or any(
            not isinstance(b, dict)
            or not _text(b.get("dir"))
            or not all(isinstance(v, str) for v in b.values())
            for b in bases.values()
        )
    ):
        raise CorpusError("TASK_BASE", "manifest bases must map a base id to {dir, ...} strings")
    return manifest


def hidden_test_functions(files: dict[str, bytes]) -> set[str]:
    """``def test…`` names of the Python files (hidden_map keys and leak-index tokens, §10.4)."""
    return {
        m.group(1).decode()
        for name, data in files.items()
        if name.endswith(".py")
        for m in TEST_FUNCTION.finditer(data)
    }


def _check_source(task_id: str, source: Any, in_tb2_dir: bool) -> None:
    if (
        not isinstance(source, dict)
        or set(source) != {"kind", "ref", "author", "created"}
        or source["kind"] not in SOURCE_KINDS
        or not all(_text(source[k]) for k in ("ref", "author", "created"))
    ):
        raise CorpusError("TASK_META", f"{task_id}: source needs kind, ref, author, created")
    try:
        date.fromisoformat(source["created"])
    except ValueError as exc:
        raise CorpusError("TASK_META", f"{task_id}: source.created is YYYY-MM-DD") from exc
    if (source["kind"] == "tb2") != in_tb2_dir:
        raise CorpusError("TASK_META", f"{task_id}: tb2 tasks live under tb2/, others under tasks/")


def _check_hidden_map(
    task_id: str, hidden_map: Any, acceptance: tuple[str, ...], functions: set[str], own: bool
) -> dict[str, int]:
    if not isinstance(hidden_map, dict) or any(
        not isinstance(k, str)
        or type(v) is not int
        or not 0 <= v < len(acceptance)
        or k not in functions
        for k, v in hidden_map.items()
    ):
        raise CorpusError(
            "TASK_HIDDEN_MAP", f"{task_id}: hidden_map maps hidden test functions to acceptance"
        )
    # §10.3 rule 2 is an own-task authoring rule: every hidden test function is mapped.
    if own and set(hidden_map) != functions:
        missing = sorted(functions - set(hidden_map))
        raise CorpusError("TASK_HIDDEN_MAP", f"{task_id}: unmapped hidden tests {missing}")
    return dict(hidden_map)


def _check_ambiguity(task: TaskV2) -> None:
    value = task.ambiguity
    if (value is not None) != (task.domain == "ambiguity"):
        raise CorpusError(
            "TASK_AMBIGUITY", f"{task.task_id}: ambiguity object iff domain ambiguity"
        )
    alt = bool(task.reference_alt) or bool(task.hidden_alt)
    if value is None:
        if alt:
            raise CorpusError("TASK_AMBIGUITY", f"{task.task_id}: *_alt only for ambiguity tasks")
        return
    if (
        not isinstance(value, dict)
        or set(value) != AMBIGUITY_KEYS
        or value["expected"] not in ("ask", "proceed")
        or not isinstance(value["must_mention_any"], list)
        or not all(_text(s) for s in value["must_mention_any"])
        or not isinstance(value["interpretations"], list)
        or len(value["interpretations"]) != 2
        or not all(_text(s) for s in value["interpretations"])
    ):
        raise CorpusError("TASK_AMBIGUITY", f"{task.task_id}: malformed ambiguity object")
    if value["expected"] == "ask":
        # §10.3: two references and two hidden suites prove the ambiguity.
        if not (task.reference_alt and task.hidden_alt and value["must_mention_any"]):
            raise CorpusError(
                "TASK_AMBIGUITY",
                f"{task.task_id}: expected ask needs reference_alt/, hidden_alt/, must_mention_any",
            )
    elif alt:
        raise CorpusError("TASK_AMBIGUITY", f"{task.task_id}: a proceed control has no *_alt")


def _load_task(folder: Path, bases: dict[str, Any], in_tb2_dir: bool) -> TaskV2:
    meta = _json(folder / "task.json", "TASK_META")
    name = folder.name
    if not isinstance(meta, dict) or set(meta) != TASK_KEYS:
        raise CorpusError("TASK_META", f"{name}: task.json v2 fields are {sorted(TASK_KEYS)}")
    task_id = meta["task_id"]
    if task_id != name or not isinstance(task_id, str) or not ID.fullmatch(task_id):
        raise CorpusError("TASK_META", f"{name}: task_id must equal the directory name")
    if meta["version"] != 2 or meta["difficulty_declared"] is not None:
        # D-098: difficulty is measured by calibration, never declared.
        raise CorpusError("TASK_META", f"{task_id}: version 2 and difficulty_declared null")
    if meta["domain"] not in DOMAINS:
        raise CorpusError("TASK_DOMAIN", f"{task_id}: unknown domain {meta['domain']!r}")
    if meta["subdomain"] is not None and not _text(meta["subdomain"]):
        raise CorpusError("TASK_META", f"{task_id}: subdomain is a string or null")
    if meta["set"] not in SETS:
        raise CorpusError("TASK_META", f"{task_id}: set is main or regression")
    if meta["split"] is not None:
        raise CorpusError("TASK_SPLIT", f"{task_id}: authors never set split (§10.2)")
    _check_source(task_id, meta["source"], in_tb2_dir)
    kind = meta["source"]["kind"]
    if meta["license"] not in LICENSES or (kind == "tb2" and meta["license"] != "Apache-2.0"):
        raise CorpusError("TASK_LICENSE", f"{task_id}: license {meta['license']!r}")
    if meta["base"] not in bases:
        raise CorpusError("TASK_BASE", f"{task_id}: base {meta['base']!r} is not in the manifest")
    environment = meta["environment"]
    if not isinstance(environment, str) or not ID.fullmatch(environment):
        raise CorpusError("TASK_META", f"{task_id}: environment is 'app' or an environment id")
    grading = meta["grading"]
    if (
        grading not in GRADINGS
        or (grading == "tb2_tests") != (kind == "tb2")
        or (grading == "planner_questions") != (meta["ambiguity"] is not None)
    ):
        raise CorpusError("TASK_GRADING", f"{task_id}: grading {grading!r} does not fit the task")
    acceptance = meta["acceptance"]
    if (
        not _text(meta["objective"])
        or not isinstance(acceptance, list)
        or not acceptance
        or not all(_text(s) for s in acceptance)
    ):
        raise CorpusError("TASK_META", f"{task_id}: objective and acceptance are required")
    if in_tb2_dir:
        hidden, reference = _files(folder / "tests", Path("tests")), _files(folder / "solution")
        hidden_alt, reference_alt = {}, {}
        tb2: dict[str, Any] | None = {"dir": f"tb2/{name}"}
    else:
        hidden = _files(folder / "hidden", HIDDEN_DIR / task_id)
        hidden_alt = _files(folder / "hidden_alt", HIDDEN_DIR / task_id)
        reference, reference_alt = _files(folder / "reference"), _files(folder / "reference_alt")
        tb2 = None
    if not hidden:
        raise CorpusError("TASK_HIDDEN", f"{task_id}: no hidden tests")
    hidden_map = _check_hidden_map(
        task_id,
        meta["hidden_map"],
        tuple(acceptance),
        hidden_test_functions(hidden) | hidden_test_functions(hidden_alt),
        kind == "own",
    )
    task = TaskV2(
        task_id,
        meta["domain"],
        "validation" if meta["set"] == "regression" else None,  # §10.6
        meta["set"],
        dict(meta["source"]),
        meta["license"],
        meta["base"],
        environment,
        grading,
        meta["objective"],
        tuple(acceptance),
        hidden,
        reference,
        reference_alt,
        hidden_map,
        deepcopy(meta["ambiguity"]),
        tb2,
        hidden_alt,
    )
    _check_ambiguity(task)
    return task


def _apply_splits(root: Path, seed: int | None, tasks: list[TaskV2]) -> list[TaskV2]:
    path = root / "splits.json"
    if not path.exists():
        return tasks
    data = _json(path, "TASK_SPLIT")
    if (
        not isinstance(data, dict)
        or set(data) != {"seed", "method", "assignments"}
        or type(data["seed"]) is not int
        or data["method"] != SPLIT_METHOD
        or not isinstance(data["assignments"], dict)
        or (seed is not None and data["seed"] != seed)
    ):
        raise CorpusError("TASK_SPLIT", "splits.json: seed (= manifest split_seed), method, map")
    by_id = {t.task_id: t for t in tasks}
    for task_id, split in data["assignments"].items():
        task = by_id.get(task_id)
        if task is None or task.set != "main" or split not in SPLITS:
            raise CorpusError("TASK_SPLIT", f"splits.json: bad assignment {task_id!r}: {split!r}")
        if split == "holdout" and task.source["kind"] != "own":
            raise CorpusError("TASK_SPLIT", f"{task_id}: only own tasks go to holdout (D-098)")
    return [
        replace(t, split=data["assignments"].get(t.task_id)) if t.set == "main" else t
        for t in tasks
    ]


def load(root: Path) -> CorpusV2:
    """The corpus from its manifest and a scan of ``tasks/*/task.json`` and ``tb2/*/task.json``."""
    root = Path(root)
    manifest = _manifest(root)
    tasks: list[TaskV2] = []
    for folder_name, in_tb2 in (("tasks", False), ("tb2", True)):
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for task_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
            if task_dir.name == "__pycache__":
                continue
            if not (task_dir / "task.json").is_file():
                raise CorpusError("TASK_META", f"{task_dir}: no task.json")
            tasks.append(_load_task(task_dir, manifest["bases"], in_tb2))
    if len({t.task_id for t in tasks}) != len(tasks):
        raise CorpusError("TASK_DUPLICATE", "Task ids must be unique")
    seed = manifest.get("split_seed")
    tasks = _apply_splits(root, seed, tasks)
    return CorpusV2(
        manifest["corpus_id"],
        manifest["version"],
        root,
        {k: dict(v) for k, v in manifest["bases"].items()},
        tuple(tasks),
        seed,
    )


def for_set(corpus: CorpusV2, set_name: str) -> CorpusV2:
    """The tasks of one set as a corpus of its own; the regression set is ``amplai-regression-v1``
    with every case in validation (§10.6)."""
    if set_name not in SETS:
        raise CorpusError("TASK_META", f"Unknown set {set_name!r}")
    tasks = tuple(t for t in corpus.tasks if t.set == set_name)
    corpus_id = REGRESSION_CORPUS_ID if set_name == "regression" else corpus.corpus_id
    return replace(corpus, corpus_id=corpus_id, tasks=tasks)


# -- splits --------------------------------------------------------------------------------------


def _share(count: int) -> int:
    """30 % of ``count``, rounded half up. §10.2 does not fix the per-domain rounding."""
    return (3 * count + 5) // 10


def assign_splits(tasks: list[TaskV2], *, seed: int) -> dict[str, str]:
    """``stratified_by_domain_v1`` over the main set (§10.2).

    Per domain: holdout = 30 % of the own tasks (TB2 and imported tasks never go to holdout),
    validation = 30 % of the remaining main tasks, development = the rest. Each domain draws from
    its own ``Random(f"{seed}:{domain}")`` over the ids in sorted order, so adding tasks to one
    domain never reshuffles another. Regression-set tasks are not assigned (§10.6).
    """
    if type(seed) is not int:
        raise CorpusError("TASK_SPLIT", "The split seed is an integer")
    if len({t.task_id for t in tasks}) != len(tasks):
        raise CorpusError("TASK_DUPLICATE", "Task ids must be unique")
    by_domain: dict[str, list[TaskV2]] = {}
    for task in tasks:
        if task.set == "main":
            by_domain.setdefault(task.domain, []).append(task)
    assignments: dict[str, str] = {}
    counts: dict[str, dict[str, int]] = {s: {} for s in SPLITS}
    for domain in sorted(by_domain):
        rng = random.Random(f"{seed}:{domain}")
        ids = sorted(t.task_id for t in by_domain[domain])
        own = sorted(t.task_id for t in by_domain[domain] if t.source.get("kind") == "own")
        rng.shuffle(own)
        holdout = set(own[: _share(len(own))])
        rest = [i for i in ids if i not in holdout]
        rng.shuffle(rest)
        validation = set(rest[: _share(len(rest))])
        for task_id in ids:
            split = (
                "holdout"
                if task_id in holdout
                else "validation"
                if task_id in validation
                else "development"
            )
            assignments[task_id] = split
            counts[split][domain] = counts[split].get(domain, 0) + 1
    holdout_n, validation_n = sum(counts["holdout"].values()), sum(counts["validation"].values())
    if holdout_n < HOLDOUT_MIN or validation_n < VALIDATION_MIN:
        raise CorpusError(
            "SPLIT_TOO_SMALL",
            f"holdout {holdout_n} (min {HOLDOUT_MIN}), validation {validation_n} "
            f"(min {VALIDATION_MIN}); counts by split and domain: {json.dumps(counts)}",
        )
    return {t.task_id: assignments[t.task_id] for t in tasks if t.task_id in assignments}


# -- grading -------------------------------------------------------------------------------------


def _shim(task: TaskV2, hidden: dict[str, bytes]) -> local_corpus.CorpusTask:
    return local_corpus.CorpusTask(
        task.task_id, "", task.objective, task.acceptance, hidden, task.reference
    )


def grade(
    task: TaskV2,
    workspace: Path,
    *,
    planner_questions: list[str] | None = None,
    timeout: int = 300,
) -> Outcome:
    """One trial's outcome on ``workspace`` (a scratch copy of the result, never the agent's tree).

    ``pytest_hidden``: ``local_corpus.judge`` with the task's hidden files (§8.3).
    ``planner_questions``: expected ``ask`` succeeds when the planner asked and some question
    names a ``must_mention_any`` string (case-insensitive); expected ``proceed`` succeeds when it
    asked nothing and the hidden tests pass (§8.3). An ``ask`` grade runs no test suite.
    ``tb2_tests``: graded in the task image (§10.5); its test entry command is 확인 필요 (§14 Q7).
    """
    if task.grading == "pytest_hidden":
        return local_corpus.judge(_shim(task, task.hidden), workspace, timeout=timeout)
    if task.grading == "tb2_tests":
        raise CorpusError(
            "TASK_GRADING", f"{task.task_id}: tb2_tests grading runs in the task image (§14 Q7)"
        )
    if planner_questions is None or task.ambiguity is None:
        raise CorpusError(
            "TASK_GRADING", f"{task.task_id}: planner_questions grading needs the questions"
        )
    if task.ambiguity["expected"] == "ask":
        # Whole words or phrases, not substrings: "ratio" must not match "configuration"
        # (authoring review 2026-10-01).
        wanted = [
            re.compile(r"(?<![a-z0-9])" + re.escape(s.lower()) + r"(?![a-z0-9])")
            for s in task.ambiguity["must_mention_any"]
        ]
        named = any(w.search(q.lower()) for q in planner_questions for w in wanted)
        asked = bool(planner_questions) and named
        detail = f"expected ask: {len(planner_questions)} questions, names a required term={named}"
        return Outcome(asked, True, detail)
    if planner_questions:
        return Outcome(False, True, f"expected proceed: {len(planner_questions)} questions asked")
    return local_corpus.judge(_shim(task, task.hidden), workspace, timeout=timeout)


# -- fairness (§10.3 rule 1, ambiguity proof) ----------------------------------------------------


def base_dir(corpus: CorpusV2, task: TaskV2) -> Path:
    return corpus.root / corpus.bases[task.base_id]["dir"]


def _verdict(
    task: TaskV2,
    base: Path,
    overlay: dict[str, bytes],
    hidden: dict[str, bytes],
    scratch: Path,
    label: str,
    repeats: int,
) -> tuple[dict[str, bool], bool, str]:
    """Judge ``base`` + ``overlay`` against ``hidden`` ``repeats`` times; flaky if it changes."""
    seen: set[tuple[bool, bool]] = set()
    outcome: Outcome | None = None
    for i in range(repeats):
        work = scratch / task.task_id / f"{label}-{i}"
        shutil.copytree(base, work, dirs_exist_ok=True)
        local_corpus.write_files(work, overlay)
        outcome = local_corpus.judge(_shim(task, hidden), work)
        seen.add((outcome.hidden_passed, outcome.visible_passed))
    assert outcome is not None
    return (
        {"hidden_passed": outcome.hidden_passed, "visible_passed": outcome.visible_passed},
        len(seen) > 1,
        outcome.detail,
    )


def _validate_task(corpus: CorpusV2, task: TaskV2, scratch: Path, repeats: int) -> dict[str, Any]:
    row: dict[str, Any] = {"grading": task.grading, "repeats": repeats}
    if task.grading == "tb2_tests":
        # TB2 fairness is the adapter's admission run in the task image (§10.5 step 4, S7a).
        return {**row, "fair": None, "flaky": False, "detail": "tb2_tests: checked by tb2.admit"}
    base = base_dir(corpus, task)
    if not base.is_dir():
        raise CorpusError("TASK_BASE", f"{task.task_id}: base tree {base} is missing")
    row["base"], flaky_base, _ = _verdict(task, base, {}, task.hidden, scratch, "base", repeats)
    row["reference"], flaky_ref, row["detail"] = _verdict(
        task, base, task.reference, task.hidden, scratch, "reference", repeats
    )
    flaky = flaky_base or flaky_ref
    fair = (
        not row["base"]["hidden_passed"]
        and row["base"]["visible_passed"]
        and row["reference"]["hidden_passed"]
        and row["reference"]["visible_passed"]
    )
    if task.ambiguity is not None and task.ambiguity["expected"] == "ask":
        # §10.3: each reference satisfies the text (its own hidden suite) and fails the other's.
        alt, f1, _ = _verdict(
            task, base, task.reference_alt, task.hidden_alt, scratch, "reference_alt", repeats
        )
        ref_on_alt, f2, _ = _verdict(
            task, base, task.reference, task.hidden_alt, scratch, "reference-on-alt", repeats
        )
        alt_on_ref, f3, _ = _verdict(
            task, base, task.reference_alt, task.hidden, scratch, "alt-on-reference", repeats
        )
        proven = (
            alt["hidden_passed"]
            and alt["visible_passed"]
            and not ref_on_alt["hidden_passed"]
            and not alt_on_ref["hidden_passed"]
        )
        row["ambiguity"] = {
            "reference_alt": alt,
            "reference_on_hidden_alt": ref_on_alt,
            "reference_alt_on_hidden": alt_on_ref,
            "proven": proven,
        }
        flaky = flaky or f1 or f2 or f3
        fair = fair and proven
    row["flaky"] = flaky
    row["fair"] = fair and not flaky
    return row


def validate(
    corpus: CorpusV2, scratch: Path, *, workers: int = 4, repeats: int = 1
) -> dict[str, dict[str, Any]]:
    """Per task: fair as ``local_corpus.validate`` (D-094 repeats) plus the ambiguity proof.

    ``fair`` is ``None`` for ``tb2_tests`` tasks: their fairness is the admission run (§10.5).
    """
    if repeats < 1:
        raise CorpusError("REPEATS", "repeats must be at least 1")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(
            pool.map(lambda t: _validate_task(corpus, t, Path(scratch), repeats), corpus.tasks)
        )
    return {task.task_id: row for task, row in zip(corpus.tasks, rows, strict=True)}


# -- freeze and index ----------------------------------------------------------------------------


def _base_files(
    corpus: CorpusV2, base_id: str, cache: dict[str, dict[str, bytes]]
) -> dict[str, bytes]:
    if base_id not in cache:
        cache[base_id] = _files(corpus.root / corpus.bases[base_id]["dir"])
    return cache[base_id]


def _reference_diff(reference: dict[str, bytes], base: dict[str, bytes]) -> dict[str, int]:
    """Changed files and ``+``/``-`` lines of the reference over its base tree (§10.3 rule 6)."""
    files = lines = 0
    for name, data in sorted(reference.items()):
        before = base.get(name)
        if before == data:
            continue
        files += 1
        old = before.decode(errors="replace").splitlines() if before is not None else []
        diff = difflib.unified_diff(old, data.decode(errors="replace").splitlines(), lineterm="")
        lines += sum(1 for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---")))
    return {"files": files, "lines": lines}


def base_commit(corpus: CorpusV2, task: TaskV2) -> str:
    """The task's base commit: the manifest's ``base_commit``, the base's ``commit_file`` or its
    per-task ``commits_file`` entry (§10.1)."""
    base = corpus.bases[task.base_id]
    commit: Any = None
    try:
        if "base_commit" in base:
            commit = base["base_commit"]
        elif "commit_file" in base:
            commit = (corpus.root / base["commit_file"]).read_text().strip()
        elif "commits_file" in base:
            commit = json.loads((corpus.root / base["commits_file"]).read_text()).get(task.task_id)
    except (OSError, ValueError) as exc:
        raise CorpusError("TASK_BASE", f"{task.task_id}: base commit unreadable") from exc
    if not isinstance(commit, str) or not SHA1.fullmatch(commit):
        raise CorpusError("TASK_BASE", f"{task.task_id}: no base commit for {task.task_id}")
    return commit


def hidden_digest(task: TaskV2) -> str:
    """As ``local_executor.corpus_cases``: the digest of the hidden files, never the files."""
    return digest({k: v.hex() for k, v in sorted(task.hidden.items())})


def case_payload(corpus: CorpusV2, task: TaskV2) -> dict[str, Any]:
    """The §2.7 case payload artifact of one task."""
    return {
        "case_id": task.task_id,
        "corpus_id": corpus.corpus_id,
        "corpus_version": corpus.version,
        "domain": task.domain,
        "set": task.set,
        "source": {"kind": task.source["kind"], "ref": task.source["ref"]},
        "license": task.license,
        "base_id": task.base_id,
        "base_commit": base_commit(corpus, task),
        "environment_id": task.environment_id,
        "grading": task.grading,
        "contract_text": task.contract_text(),
        "hidden_digest": hidden_digest(task),
    }


def _check_freezable(corpus: CorpusV2) -> str:
    sets = {t.set for t in corpus.tasks}
    if len(sets) != 1:
        raise CorpusError("TASK_SPLIT", "Freeze one set per corpus (§10.6); use for_set")
    (set_name,) = sets
    if (set_name == "regression") != (corpus.corpus_id == REGRESSION_CORPUS_ID):
        raise CorpusError("TASK_SPLIT", f"The {set_name} set freezes as its own corpus (§10.6)")
    for task in corpus.tasks:
        if task.split not in SPLITS:
            raise CorpusError("TASK_SPLIT", f"{task.task_id}: no split assigned")
        if set_name == "regression" and task.split != "validation":
            raise CorpusError("TASK_SPLIT", f"{task.task_id}: regression cases are validation")
        if task.split == "holdout" and task.source["kind"] != "own":
            raise CorpusError("TASK_SPLIT", f"{task.task_id}: only own tasks go to holdout")
    if set_name == "main" and type(corpus.split_seed) is not int:
        raise CorpusError("TASK_SPLIT", "The main corpus needs the manifest split_seed")
    return set_name


def validate_task_index(value: dict[str, Any]) -> None:
    """Shape of a ``corpus-task-index`` record (§2.7) before ``put``."""
    rows = value.get("tasks")
    splits = value.get("splits")
    if (
        value.get("schema") != "amplai.corpus-task-index.v1"
        or not isinstance(value.get("corpus_ref"), dict)
        or not isinstance(rows, list)
        or not isinstance(splits, dict)
        or set(splits) != {"seed", "method", "counts"}
        or any(
            not isinstance(r, dict)
            or r.get("split") not in SPLITS
            or r.get("domain") not in DOMAINS
            for r in rows
        )
    ):
        raise RuntimeFault("TASK_INDEX", "Malformed corpus-task-index record")


def _next_revision(db: sqlite3.Connection, actor: Actor, kind: str, object_id: str) -> int:
    """Latest stored revision of ``object_id`` + 1, read inside the writing transaction (§2.0: a
    new version is a new revision of the same id; as ``runtime/goals/service.py:380-384``)."""
    latest = db.execute(
        "SELECT MAX(revision) FROM objects WHERE tenant=? AND project=? AND kind=? AND id=?",
        (*actor.scope.keys(), kind, object_id),
    ).fetchone()[0]
    return 1 if latest is None else int(latest) + 1


def freeze(
    actor: Actor,
    store: Store,
    artifacts: ArtifactStore,
    corpus: CorpusV2,
    *,
    holdout_use_limit: int,
) -> dict[str, Ref]:
    """Freeze one set of the corpus into ``CorpusService`` plus its task index and leak index.

    The eval-corpus id is ``<corpus_id>-<version>``: ``CorpusService.freeze`` always writes
    revision 1 (``evaluation/corpus.py:124-125``), so each version needs its own id there (a
    forced deviation from §2.0 versioning). The index records follow §2.7 and §2.0: ids
    ``taskindex-<corpus_id>`` and ``leak-<corpus_id>``, a new version being the next revision.
    """
    from .leak_gate import build_index, validate_index

    actor.require("corpus.manage")
    if "harness.propose" in actor.permissions:
        # As CorpusService.freeze (evaluation/corpus.py:35-40), before any artifact is admitted.
        raise Hold("CORPUS_PROPOSER", "Proposers may not publish protected corpus versions")
    set_name = _check_freezable(corpus)
    frozen_id = f"{corpus.corpus_id}-{corpus.version}"
    task_index_id, leak_index_id = f"taskindex-{corpus.corpus_id}", f"leak-{corpus.corpus_id}"
    if not ID.fullmatch(frozen_id) or not ID.fullmatch(task_index_id):
        raise CorpusError("TASK_META", f"Corpus id {frozen_id!r} is not a record id")
    cache: dict[str, dict[str, bytes]] = {}
    cases, rows = [], []
    counts: dict[str, dict[str, int]] = {}
    for task in corpus.tasks:
        payload = case_payload(corpus, task)
        assert task.split is not None
        cases.append(
            {
                "case_id": task.task_id,
                "split": task.split,
                "task_class": task.domain,  # §2.7: task_class is the corpus v2 domain
                "artifact_ref": artifacts.admit(
                    actor.scope, canonical(payload), "application/json", trust="operator"
                ),
            }
        )
        rows.append(
            {
                "case_id": task.task_id,
                "domain": task.domain,
                "split": task.split,
                "set": task.set,
                "source_kind": task.source["kind"],
                "license": task.license,
                "grading": task.grading,
                "environment_id": task.environment_id,
                "acceptance_count": len(task.acceptance),
                "reference_diff": _reference_diff(
                    task.reference, _base_files(corpus, task.base_id, cache)
                ),
            }
        )
        by_domain = counts.setdefault(task.split, {})
        by_domain[task.domain] = by_domain.get(task.domain, 0) + 1
    corpus_ref = CorpusService(store, artifacts).freeze(
        actor, frozen_id, cases, holdout_use_limit=holdout_use_limit
    )
    index = {
        "schema": "amplai.corpus-task-index.v1",
        "scope": actor.scope.wire(),
        "corpus_ref": corpus_ref,
        "corpus_version": corpus.version,
        "tasks": rows,
        "splits": {
            "seed": corpus.split_seed,
            "method": SPLIT_METHOD if set_name == "main" else REGRESSION_METHOD,
            "counts": counts,
        },
    }
    leak = {
        "schema": "amplai.leak-index.v1",
        "scope": actor.scope.wire(),
        "corpus_ref": corpus_ref,
        **build_index(corpus, {t.task_id: t.split for t in corpus.tasks if t.split}),
    }
    validate_task_index(index)
    validate_index(leak)
    with store.tx() as db:
        task_index_ref = store.put(
            db,
            actor.scope,
            "corpus-task-index",
            task_index_id,
            _next_revision(db, actor, "corpus-task-index", task_index_id),
            index,
        )
        leak_index_ref = store.put(
            db,
            actor.scope,
            "leak-index",
            leak_index_id,
            _next_revision(db, actor, "leak-index", leak_index_id),
            leak,
        )
    return {
        "corpus_ref": corpus_ref,
        "task_index_ref": task_index_ref,
        "leak_index_ref": leak_index_ref,
    }


def index_for(actor: Actor, store: Store, task_index_ref: Ref) -> list[dict[str, Any]]:
    """Task-index rows; a ``harness.propose`` actor sees development rows only (the rule of
    ``evaluation/corpus.py:131-132``)."""
    actor.require("corpus.read")
    value = store.get(actor.scope, "corpus-task-index", task_index_ref)
    validate_task_index(value)
    rows: list[dict[str, Any]] = deepcopy(value["tasks"])
    if "harness.propose" in actor.permissions:
        rows = [r for r in rows if r["split"] == "development"]
    return rows


# -- Work 030 regression import (§10.6) ----------------------------------------------------------


def _copy_tree(src: Path, dest: Path) -> None:
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))


def import_work030(src: Path, dest: Path) -> list[str]:
    """Copy the Work 030 tasks as ``work030-<id>`` regression tasks on the ``demo`` base.

    Also copies the Work 030 ``base/`` to ``bases/demo/`` when it is absent, and refuses a
    present one that differs. Refuses a task directory that already exists (``TASK_DUPLICATE``).
    ``hidden_map`` is left empty: the Work 030 tasks have no map and §10.3 rule 2 binds own tasks.
    """
    src, dest = Path(src), Path(dest)
    manifest = _json(src / "manifest.json", "TASK_META")
    base_dest = dest / "bases" / "demo"
    if base_dest.exists():
        if _files(base_dest) != _files(src / "base"):
            raise CorpusError("TASK_BASE", f"{base_dest} differs from the Work 030 base")
    else:
        _copy_tree(src / "base", base_dest)
    imported = []
    for old_id in manifest["task_ids"]:
        folder = src / "tasks" / old_id
        meta = _json(folder / "task.json", "TASK_META")
        task_id = f"work030-{old_id}"
        target = dest / "tasks" / task_id
        if target.exists():
            raise CorpusError("TASK_DUPLICATE", f"{target} already exists")
        target.mkdir(parents=True)
        for part in ("hidden", "reference"):
            if (folder / part).is_dir():
                _copy_tree(folder / part, target / part)
        value = {
            "task_id": task_id,
            "version": 2,
            "domain": "regression",
            "subdomain": None,
            "set": "regression",
            "split": None,
            "source": {
                "kind": "work030",
                "ref": f"{manifest['corpus_id']}/{old_id}",
                "author": WORK030_AUTHOR,
                "created": WORK030_CREATED,
            },
            "license": "LicenseRef-amplai-internal",
            "base": "demo",
            "environment": "app",
            "grading": "pytest_hidden",
            "objective": meta["objective"],
            "acceptance": meta["acceptance"],
            "hidden_map": {},
            "ambiguity": None,
            "difficulty_declared": None,
        }
        (target / "task.json").write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        imported.append(task_id)
    return imported

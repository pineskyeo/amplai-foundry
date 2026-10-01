"""Elite archive per harness cell and lineage (Work 033 S13, interfaces.md §2.13, §3.12, §9.9).

One ``elite-archive`` head per cell (id ``archive-<cell_id>``): the champion (the cell's composition
in the active release), elites per (domain x cost band) and the lineage of measured candidates.

Development data only (§9.9): elites count ``trial-metrics`` rows with ``split == "development"``
(screening, ablation, nightly search, development calibration) of the cell, never validation or
holdout rows. Focused and holdout results reach the archive only as a lineage ``verdict``, which
``proposer_view`` (the proposer's only archive input, §9.4) leaves out; the same reason keeps
focused/holdout prediction scores operator-only (§9.6).

Placement, recomputed on every ``update`` from the stored development rows of the cell (plus the
rows passed in), so it does not depend on the order of updates:

- a row belongs to the composition its trial record names (``eval-trial``/``calibration-trial``
  ``composition_ref``); rows without a known result, a domain or a resolvable trial are left out;
- cost band = the tercile of the composition's tokens per solved task (development rows, all
  domains) among the cell's measured compositions, by rank (``low``/``mid``/``high``); a
  composition without a solved task or with a row of unknown token usage is ``high``;
- in each (domain, band) bucket the elite is the composition with the highest success with
  ``n >= 4`` there; ties keep the incumbent, then fewer tokens per solved task, then the id;
- hack guards reject only, never a score (§9.10): a candidate whose screening stage recorded a
  ``HACK_GUARD`` finding is never placed; guard signals never enter a ranking.

Lineage comes from proposal ids (the proposal's baseline is a parent) and component ``parent`` /
``merge_parents`` (an archived composition that holds a parent of a changed component is a parent).
Parents for the proposer: the champion and up to ``k - 1`` elites of the buckets where the
champion's development success is lowest (unmeasured counts as lowest).
"""

from __future__ import annotations

import copy
from typing import Any, TypeGuard

from ..runtime.contracts.identity import digest, now
from ..runtime.contracts.semantics import resolve_ref
from ..runtime.errors import RuntimeFault
from ..runtime.storage.store import Scope, Store

Ref = dict[str, Any]

KIND = "elite-archive"
METRICS_KIND = "trial-metrics"
TRIAL_KINDS = ("eval-trial", "calibration-trial")
BANDS = ("low", "mid", "high")
MIN_N = 4  # §9.9: an elite needs n >= 4 in its bucket
DEVELOPMENT = "development"
GUARD_FINDING = "HACK_GUARD"  # stages.py records "HACK_GUARD: <finding>" in screening findings


def _is_ref(value: Any) -> TypeGuard[Ref]:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "revision", "digest"}
        and isinstance(value["id"], str)
        and type(value["revision"]) is int
        and isinstance(value["digest"], str)
    )


def _key(ref: Ref) -> str:
    return f"{ref['id']}@{ref['revision']}"


def _ref(value: Ref) -> Ref:
    return {k: value[k] for k in ("id", "revision", "digest")}


def validate_archive(value: Any) -> None:
    """The §2.13 ``elite-archive`` head shape; RuntimeFault ELITE_ARCHIVE otherwise."""
    champion = value.get("champion") if isinstance(value, dict) else None
    ok = (
        isinstance(value, dict)
        and set(value) == {"cell_id", "champion", "elites", "lineage"}
        and isinstance(value["cell_id"], str)
        and (
            champion is None
            or (
                isinstance(champion, dict)
                and set(champion) == {"composition_ref", "since", "release_ref"}
                and _is_ref(champion["composition_ref"])
                and isinstance(champion["since"], str)
                and (champion["release_ref"] is None or _is_ref(champion["release_ref"]))
            )
        )
        and isinstance(value["elites"], list)
        and all(
            isinstance(e, dict)
            and set(e)
            == {
                "bucket",
                "composition_ref",
                "manifest_ref",
                "success",
                "n",
                "tokens_per_solved",
                "evidence",
                "added_at",
            }
            and isinstance(e["bucket"], dict)
            and set(e["bucket"]) == {"domain", "cost_band"}
            and e["bucket"]["cost_band"] in BANDS
            and _is_ref(e["composition_ref"])
            and (e["manifest_ref"] is None or _is_ref(e["manifest_ref"]))
            and isinstance(e["success"], (int, float))
            and type(e["n"]) is int
            and e["n"] >= MIN_N
            and (e["tokens_per_solved"] is None or isinstance(e["tokens_per_solved"], (int, float)))
            and isinstance(e["evidence"], list)
            and all(_is_ref(r) for r in e["evidence"])
            and isinstance(e["added_at"], str)
            for e in value["elites"]
        )
        and isinstance(value["lineage"], list)
        and all(
            isinstance(entry, dict)
            and set(entry) == {"child", "parents", "proposal_id", "verdict"}
            and _is_ref(entry["child"])
            and isinstance(entry["parents"], list)
            and all(_is_ref(p) for p in entry["parents"])
            and isinstance(entry["proposal_id"], str)
            and (entry["verdict"] is None or isinstance(entry["verdict"], str))
            for entry in value["lineage"]
        )
    )
    if not ok:
        raise RuntimeFault("ELITE_ARCHIVE", "Invalid elite-archive head (§2.13)")


class EliteArchive:
    def __init__(self, store: Store, scope: Scope) -> None:
        self.store, self.scope = store, scope

    # -- the head --
    @staticmethod
    def head_id(cell_id: str) -> str:
        return "archive-" + cell_id

    def head(self, cell_id: str) -> dict[str, Any] | None:
        """The archive head's data, or None before the first write."""
        try:
            data: dict[str, Any] = self.store.head(self.scope, KIND, self.head_id(cell_id))["data"]
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            return None
        return data

    def head_ref(self, cell_id: str) -> Ref | None:
        """A ref-shaped pointer to the head version read now: ``{"id", "revision": row_version,
        "digest"}`` (``proposer-run.inputs.archive_ref``; a head version, not a record)."""
        try:
            head = self.store.head(self.scope, KIND, self.head_id(cell_id))
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            return None
        return {
            "id": self.head_id(cell_id),
            "revision": head["row_version"],
            "digest": digest(head["data"]),
        }

    @staticmethod
    def _empty(cell_id: str) -> dict[str, Any]:
        return {"cell_id": cell_id, "champion": None, "elites": [], "lineage": []}

    def _write(self, db: Any, cell_id: str, data: dict[str, Any]) -> None:
        validate_archive(data)
        try:
            head: dict[str, Any] | None = self.store.head(
                self.scope, KIND, self.head_id(cell_id), db=db
            )
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            head = None
        if head is not None and head["data"] == data:
            return
        self.store.cas(
            db,
            self.scope,
            KIND,
            self.head_id(cell_id),
            head["row_version"] if head else 0,
            "active",
            data,
        )

    def set_champion(
        self, cell_id: str, *, composition_ref: Ref, release_ref: Ref | None
    ) -> dict[str, Any]:
        """Record the cell's champion (the composition in the active release,
        ``releases.effective``); ``since`` changes only when the champion does."""
        if not _is_ref(composition_ref) or (release_ref is not None and not _is_ref(release_ref)):
            raise RuntimeFault("ELITE_ARCHIVE", "The champion and its release are refs")
        with self.store.tx() as db:
            data = copy.deepcopy(self.head(cell_id) or self._empty(cell_id))
            current = data["champion"]
            if (
                current is None
                or current["composition_ref"] != composition_ref
                or current["release_ref"] != release_ref
            ):
                data["champion"] = {
                    "composition_ref": _ref(composition_ref),
                    "since": now()
                    if current is None or current["composition_ref"] != composition_ref
                    else current["since"],
                    "release_ref": _ref(release_ref) if release_ref else None,
                }
            self._place(cell_id, data, [])
            self._write(db, cell_id, data)
        return data

    # -- measurements (development rows only) --
    def _composition_of(self, trial_ref: Any, cache: dict[str, Ref | None]) -> Ref | None:
        if not _is_ref(trial_ref):
            return None
        key = _key(trial_ref)
        if key not in cache:
            try:
                kind, trial = resolve_ref(self.store, self.scope, trial_ref)
            except RuntimeFault:
                kind, trial = "", {}
            found = trial.get("composition_ref") if kind in TRIAL_KINDS else None
            cache[key] = _ref(found) if _is_ref(found) else None
        return cache[key]

    def development_rows(
        self, cell_id: str, extra: list[dict[str, Any]] | None = None
    ) -> list[tuple[Ref | None, dict[str, Any]]]:
        """(record ref or None, row) of every development ``trial-metrics`` row of the cell: the
        stored records (latest revision per trial) and ``extra`` rows not stored yet."""
        latest: dict[str, tuple[Ref, dict[str, Any]]] = {}
        for ref, value in self.store.list_objects(self.scope, METRICS_KIND):
            if ref["id"] not in latest or ref["revision"] > latest[ref["id"]][0]["revision"]:
                latest[ref["id"]] = (ref, value)
        rows: dict[str, tuple[Ref | None, dict[str, Any]]] = {}
        for ref, value in latest.values():
            trial = value.get("trial_ref")
            if _is_ref(trial):
                rows[_key(trial)] = (ref, value)
        for value in extra or []:
            trial = value.get("trial_ref") if isinstance(value, dict) else None
            if _is_ref(trial) and _key(trial) not in rows:
                rows[_key(trial)] = (None, value)
        return [
            (ref, value)
            for ref, value in rows.values()
            if value.get("split") == DEVELOPMENT and value.get("cell_id") == cell_id
        ]

    def measurements(
        self, cell_id: str, extra: list[dict[str, Any]] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Per measured composition: its ref and, overall and per domain, n (known results),
        solved, tokens (None once a counted row has unknown usage) and the evidence refs."""
        cache: dict[str, Ref | None] = {}
        out: dict[str, dict[str, Any]] = {}
        for ref, row in self.development_rows(cell_id, extra):
            success, domain = row.get("success"), row.get("domain")
            composition = self._composition_of(row.get("trial_ref"), cache)
            if not isinstance(success, bool) or not isinstance(domain, str) or not domain:
                continue
            if composition is None:
                continue
            tokens = row.get("tokens") or {}
            spent = (
                tokens["input"] + tokens["output"]
                if type(tokens.get("input")) is int and type(tokens.get("output")) is int
                else None
            )
            entry = out.setdefault(
                _key(composition),
                {
                    "ref": composition,
                    "n": 0,
                    "solved": 0,
                    "tokens": 0,
                    "domains": {},
                },
            )
            cell = entry["domains"].setdefault(
                domain, {"n": 0, "solved": 0, "tokens": 0, "evidence": []}
            )
            for part in (entry, cell):
                part["n"] += 1
                part["solved"] += int(success)
                part["tokens"] = (
                    part["tokens"] + spent
                    if part["tokens"] is not None and spent is not None
                    else None
                )
            if ref is not None:
                cell["evidence"].append(_ref(ref))
        return out

    @staticmethod
    def per_solved(part: dict[str, Any]) -> float | None:
        if not part["solved"] or part["tokens"] is None:
            return None
        return round(float(part["tokens"]) / int(part["solved"]), 1)

    @classmethod
    def bands(cls, measured: dict[str, dict[str, Any]]) -> dict[str, str]:
        """Composition key -> cost band: terciles of tokens per solved task by rank among the
        measured compositions; None (no solved task, unknown usage) is ``high``."""
        values = {k: cls.per_solved(v) for k, v in measured.items()}
        known = sorted(v for v in values.values() if v is not None)
        out: dict[str, str] = {}
        for key, value in values.items():
            if value is None or not known:
                out[key] = "high"
                continue
            rank = sum(1 for v in known if v < value)
            out[key] = BANDS[min(2, (3 * rank) // len(known))]
        return out

    # -- placement --
    def _guard_rejected(self, proposal_id: str) -> bool:
        """The candidate's screening stage recorded a hack-guard finding (reject only, §9.10)."""
        try:
            run = self.store.head(self.scope, "stage-run", "stagerun-" + proposal_id)
        except RuntimeFault:
            return False
        screening = (run["data"].get("stages") or {}).get("screening") or {}
        return any(
            isinstance(f, str) and f.startswith(GUARD_FINDING)
            for f in screening.get("guard_findings") or []
        )

    def _manifest_ref(self, composition_ref: Ref) -> Ref | None:
        """The ``harness-manifest`` index record of the composition's four carriers, if any."""
        try:
            value = self.store.get(self.scope, "harness-composition", composition_ref)
        except RuntimeFault:
            return None
        four = {
            k: value.get(k)
            for k in (
                "prompt_bundle_ref",
                "context_policy_ref",
                "budget_policy_ref",
                "router_policy_ref",
            )
        }
        manifest_id = "manifest-" + digest(four)[7:31]
        rows = [
            r
            for r, _ in self.store.list_objects(self.scope, "harness-manifest")
            if r["id"] == manifest_id
        ]
        return _ref(max(rows, key=lambda r: r["revision"])) if rows else None

    def _place(self, cell_id: str, data: dict[str, Any], extra: list[dict[str, Any]]) -> None:
        """Recompute ``data["elites"]`` from the development measurements (module docstring)."""
        measured = self.measurements(cell_id, extra)
        bands = self.bands(measured)
        rejected = {
            _key(entry["child"])
            for entry in data["lineage"]
            if self._guard_rejected(entry["proposal_id"])
        }
        incumbents = {(e["bucket"]["domain"], e["bucket"]["cost_band"]): e for e in data["elites"]}
        candidates: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
        for key, entry in measured.items():
            if key in rejected:
                continue
            for domain, part in entry["domains"].items():
                if part["n"] >= MIN_N:
                    candidates.setdefault((domain, bands[key]), []).append((key, part))
        elites = []
        for bucket in sorted(candidates):
            incumbent = incumbents.get(bucket)
            held = _key(incumbent["composition_ref"]) if incumbent else None

            def rank(item: tuple[str, dict[str, Any]], held: str | None = held) -> tuple[Any, ...]:
                key, part = item
                tps = self.per_solved(part)
                return (
                    -part["solved"] / part["n"],
                    key != held,
                    tps if tps is not None else float("inf"),
                    key,
                )

            key, part = min(candidates[bucket], key=rank)
            ref = measured[key]["ref"]
            elites.append(
                {
                    "bucket": {"domain": bucket[0], "cost_band": bucket[1]},
                    "composition_ref": ref,
                    "manifest_ref": self._manifest_ref(ref),
                    "success": round(part["solved"] / part["n"], 4),
                    "n": part["n"],
                    "tokens_per_solved": self.per_solved(part),
                    "evidence": list(part["evidence"]),
                    "added_at": incumbent["added_at"] if incumbent and key == held else now(),
                }
            )
        data["elites"] = elites

    # -- lineage --
    def _archived(self, data: dict[str, Any]) -> list[Ref]:
        refs = [e["composition_ref"] for e in data["elites"]]
        refs += [entry["child"] for entry in data["lineage"]]
        if data["champion"]:
            refs.append(data["champion"]["composition_ref"])
        unique: dict[str, Ref] = {}
        for ref in refs:
            unique.setdefault(_key(ref), ref)
        return list(unique.values())

    def _lineage_parents(
        self, composition_ref: Ref, proposal_id: str, data: dict[str, Any]
    ) -> list[Ref]:
        """The proposal's baseline, then every archived composition holding a ``parent`` or
        ``merge_parents`` component of a component the child changed."""
        from .components import ComponentService
        from .manifest import ManifestService

        parents: dict[str, Ref] = {}
        baseline: Ref | None = None
        try:
            head = self.store.head(self.scope, "evolution", proposal_id)
            proposal = self.store.get(
                self.scope, "harness-change-proposal", head["data"]["proposal_ref"]
            )
            baseline = _ref(proposal["baseline_ref"])
            parents[_key(baseline)] = baseline
        except (RuntimeFault, KeyError, TypeError):
            return list(parents.values())
        try:
            components = ComponentService(self.store, self.scope)
            manifests = ManifestService(self.store, self.scope, None, components)
            changes = manifests.diff(
                manifests.of_composition(baseline), manifests.of_composition(composition_ref)
            )
            wanted: set[str] = set()
            for change in changes:
                if change.after is None or change.kind == "role_prompt":
                    continue
                value = components.get(change.after)
                for ref in [value.get("parent"), *(value.get("merge_parents") or [])]:
                    if _is_ref(ref):
                        wanted.add(digest(_ref(ref)))
            if wanted:
                for ref in self._archived(data):
                    if _key(ref) == _key(composition_ref) or _key(ref) in parents:
                        continue
                    flat = manifests.of_composition(ref).flat()
                    if any(_is_ref(r) and digest(_ref(r)) in wanted for r in flat.values()):
                        parents[_key(ref)] = ref
        except (RuntimeFault, KeyError, TypeError):
            pass
        return list(parents.values())

    # -- the public operations --
    def update(
        self,
        cell_id: str,
        *,
        composition_ref: Ref,
        stage_metrics: list[dict[str, Any]],
        proposal_id: str,
        verdict: str | None,
    ) -> dict[str, Any]:
        """After an evaluated stage: the lineage entry of (``composition_ref``, ``proposal_id``)
        (its ``verdict`` when given, operator-only) and the elites recomputed from development
        rows (``stage_metrics`` rows of another split change no elite, §9.9)."""
        if not _is_ref(composition_ref) or not isinstance(proposal_id, str) or not proposal_id:
            raise RuntimeFault("ELITE_ARCHIVE", "update needs a composition ref and a proposal id")
        if verdict is not None and not isinstance(verdict, str):
            raise RuntimeFault("ELITE_ARCHIVE", "A verdict is text or None")
        extra = [
            r
            for r in stage_metrics
            if isinstance(r, dict) and r.get("split") == DEVELOPMENT and r.get("cell_id") == cell_id
        ]
        with self.store.tx() as db:
            data = copy.deepcopy(self.head(cell_id) or self._empty(cell_id))
            child = _ref(composition_ref)
            entry = next(
                (
                    e
                    for e in data["lineage"]
                    if e["child"] == child and e["proposal_id"] == proposal_id
                ),
                None,
            )
            parents = self._lineage_parents(child, proposal_id, data)
            if entry is None:
                data["lineage"].append(
                    {
                        "child": child,
                        "parents": parents,
                        "proposal_id": proposal_id,
                        "verdict": verdict,
                    }
                )
            else:
                entry["parents"] = parents
                if verdict is not None:
                    entry["verdict"] = verdict
            self._place(cell_id, data, extra)
            self._write(db, cell_id, data)
        return data

    def parents(self, cell_id: str, *, k: int = 3) -> list[Ref]:
        """The champion and up to ``k - 1`` elites of the buckets where the champion's
        development success is lowest (§9.9)."""
        if type(k) is not int or k < 1:
            raise RuntimeFault("ELITE_ARCHIVE", "k is a positive integer")
        data = self.head(cell_id) or self._empty(cell_id)
        champion = data["champion"]["composition_ref"] if data["champion"] else None
        out: list[Ref] = [champion] if champion else []
        measured = self.measurements(cell_id)
        mine = measured.get(_key(champion), {}).get("domains", {}) if champion else {}

        def weakness(elite: dict[str, Any]) -> tuple[Any, ...]:
            part = mine.get(elite["bucket"]["domain"])
            success = part["solved"] / part["n"] if part and part["n"] else -1.0
            return (success, -elite["success"], elite["added_at"], _key(elite["composition_ref"]))

        for elite in sorted(data["elites"], key=weakness):
            if len(out) >= k:
                break
            if all(_key(elite["composition_ref"]) != _key(r) for r in out):
                out.append(elite["composition_ref"])
        return out[:k]

    def proposer_view(self, cell_id: str) -> dict[str, Any]:
        """The champion, the development-derived elites and the lineage without verdicts: the
        only archive input of the proposer (§9.4)."""
        data = self.head(cell_id) or self._empty(cell_id)
        return {
            "cell_id": cell_id,
            "champion": copy.deepcopy(data["champion"]),
            "elites": copy.deepcopy(data["elites"]),
            "lineage": [
                {
                    "child": e["child"],
                    "parents": list(e["parents"]),
                    "proposal_id": e["proposal_id"],
                }
                for e in data["lineage"]
            ],
        }

    def _heads(self) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT id FROM heads WHERE tenant=? AND project=? AND kind=?",
                (*self.scope.keys(), KIND),
            ).fetchall()
        return [self.store.head(self.scope, KIND, r["id"])["data"] for r in rows]

    def lineage(self, composition_ref: Ref) -> list[dict[str, Any]]:
        """The ancestry of a composition over every cell's archive (operator query, verdicts
        included): its lineage entries, then its parents' entries, breadth first."""
        if not _is_ref(composition_ref):
            raise RuntimeFault("ELITE_ARCHIVE", "lineage needs a composition ref")
        entries = [e for data in self._heads() for e in data["lineage"]]
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        queue = [_key(composition_ref)]
        while queue:
            key = queue.pop(0)
            if key in seen:
                continue
            seen.add(key)
            for entry in entries:
                if _key(entry["child"]) == key:
                    out.append(copy.deepcopy(entry))
                    queue += [_key(p) for p in entry["parents"]]
        return out

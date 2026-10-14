"""Publish a verified change as a branch and a draft PR (D-071, R8).

Preconditions, all re-checked here rather than trusted from the caller: the goal is
``verified`` with a stored goal verification, the operator approval is present, not revoked
and consents to this publication mode, and the change is the verified work output.

The commit is built with git plumbing only (temporary index: read-tree base, apply --cached,
write-tree, commit-tree), so the operator's working tree, index and current branch are never
touched and ``main`` is never written. The branch ``amplai/<goal>`` is pushed without force; a
remote branch at another commit is a conflict, not an overwrite.

The push and the draft PR go through the configured ``git_host`` module (Work 034 OD-15,
``runtime/git_host/``); this file names no code host. Without a module, publication holds with
``PUBLISH_NO_GIT_HOST``.

The durable ``publication`` record follows the effect rules: ``prepared`` → ``pushed`` →
``done``; an interrupted step leaves the record where it was and the next call reconciles
against the remote (existing branch at the same commit, existing PR for the head) instead of
repeating the side effect. (EffectService governs in-run tool effects of a running work; a
publication happens after ``goal.verified``, when no run is active.)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from ..contracts.identity import digest, now
from ..errors import Conflict, Hold, RuntimeFault
from . import text_bounds
from .product import PORT, LocalExecutionService

if TYPE_CHECKING:
    from ..git_host import GitHost

KIND = "publication"


def terminal_nodes(
    graph: dict[str, Any], node_apps: dict[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    """The last node of each app in dependency order (Work 033 S9, interfaces.md §3.3), keyed by
    app in first-node order.

    A strategy that splits one app's change (M5) leaves a chain (``node-<app>.s<k>``) or parts and
    an integration node (``.p<k>``, ``.int``); the last node's verified change is cumulative from
    the base commit, so it is the app's whole change. ``node_apps`` maps node ids to apps (the plan
    record's); without it a node id is ``node-<app>``."""
    nodes = graph["nodes"]
    by_id = {n["node_id"]: n for n in nodes}
    names = node_apps or {}

    def app_of(node: dict[str, Any]) -> str:
        node_id = node["node_id"]
        return names.get(node_id) or node_id[len("node-") :]

    # depth-first topological order (the compiler refuses cycles, validate_graph)
    order: list[str] = []
    seen: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in seen or node_id not in by_id:
            return
        seen.add(node_id)
        for dep in by_id[node_id].get("depends_on") or []:
            visit(dep)
        order.append(node_id)

    for node in nodes:
        visit(node["node_id"])
    last: dict[str, dict[str, Any]] = {}
    for node_id in order:
        last[app_of(by_id[node_id])] = by_id[node_id]
    first = list(dict.fromkeys(app_of(n) for n in nodes))
    return {app: last[app] for app in first}


def _run(
    args: list[str], *, cwd: Path, env: dict[str, str] | None = None, input: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args, cwd=cwd, env=env, input=input, capture_output=True, timeout=300, check=False
    )


NO_GIT_HOST = "PUBLISH_NO_GIT_HOST"


def _no_host(*_: object) -> NoReturn:
    raise Hold(NO_GIT_HOST, "No git_host module is configured; publication holds")


class GitPublisher:
    """Publishes through the configured ``git_host`` module (Work 034 OD-15): it pushes the
    branch, opens the draft PR and links the PRs of a multi-app goal. ``git_host=None`` (no
    module configured) holds every publication with ``PUBLISH_NO_GIT_HOST`` before anything is
    committed or recorded. ``pr_creator`` / ``pr_linker`` replace the module's PR operations
    (test stand-ins); the push always goes through the module."""

    def __init__(
        self,
        service: LocalExecutionService,
        *,
        git_host: GitHost | None,
        pr_creator: Callable[[Path, str, str, str, str], str] | None = None,
        pr_linker: Callable[[Path, str, str], None] | None = None,
    ) -> None:
        self.service, self.git_host = service, git_host
        self.pr_creator = pr_creator
        self.pr_linker = pr_linker or (git_host.comment_pr if git_host else _no_host)
        self.store, self.scope = service.store, service.scope

    # -- checks --------------------------------------------------------------------------------
    def _authorized(self, goal_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        plan = self.service.plan_record(goal_id)
        goal = self.store.head(self.scope, "goal", goal_id)
        if goal["state"] != "verified":
            raise Hold("PUBLISH_NOT_VERIFIED", "Only a verified goal is published")
        decision_ref = plan.get("decision_ref")
        if not decision_ref:
            raise Hold("PUBLISH_NOT_APPROVED", "No operator approval for this goal")
        decision = self.service.authority.resolver(self.scope, decision_ref)
        if decision.get("revoked") or decision.get("status") != "approved":
            raise Hold("PUBLISH_REVOKED", "The operator approval was revoked")
        consent = decision.get("publish") or {}
        if consent.get("mode") not in {"draft_pr", "branch"}:
            raise Hold("PUBLISH_NOT_CONSENTED", "The approval does not consent to publication")
        return plan, decision

    def _change(self, node: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        work = self.store.head(self.scope, "work", node["work_id"])
        if work["state"] != "succeeded":
            raise Hold("PUBLISH_WORK_STATE", "The verified work did not succeed")
        change = work["data"]["outputs"][PORT]
        raw = self.service.workspaces.artifacts.read(self.scope, change)
        base, patch = self.service.workspaces.read_change(self.scope, raw)
        value = json.loads(self.service.workspaces.artifacts.read(self.scope, base))
        return value, patch

    # -- effect record -------------------------------------------------------------------------
    def _record(self, record_id: str) -> dict[str, Any] | None:
        try:
            return self.store.head(self.scope, KIND, record_id)
        except RuntimeFault:
            return None

    def _save(self, record_id: str, state: str, value: dict[str, Any]) -> None:
        with self.store.tx() as db:
            current = self._record(record_id)
            version = current["row_version"] if current else 0
            self.store.cas(db, self.scope, KIND, record_id, version, state, value)

    # -- publish -------------------------------------------------------------------------------
    def __call__(self, goal_id: str) -> dict[str, Any]:
        plan, decision = self._authorized(goal_id)
        if self.git_host is None:
            _no_host()
        graph = self.store.get(self.scope, "workgraph", plan["graph_ref"])
        # the last node of each app (Work 033 S9): its change holds every part of that app
        terminal = terminal_nodes(graph, plan.get("node_apps"))
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", goal_id)
        if len(terminal) == 1:
            node = next(iter(terminal.values()))
            return self._publish(
                goal_id, plan, decision, node, plan["app"], goal_id, "amplai/" + safe, []
            )
        # one draft PR per app (D-081), linked both ways. Every app's remote is checked first, so a
        # remote on another host stops the goal before any app is pushed (git_host check_remote).
        for app in terminal:
            current = self._record(f"{goal_id}:{app}")
            if current is not None and current["state"] == "done":
                continue
            remote = (current or {}).get("data", {}).get("remote") or self._consent(decision, app)[
                "remote"
            ]
            self.git_host.check_remote(self.service.apps[app].config.repo, remote)
        published: dict[str, dict[str, Any]] = {}
        for app, node in terminal.items():
            earlier = [v["pr_url"] for v in published.values() if v.get("pr_url")]
            published[app] = self._publish(
                goal_id, plan, decision, node, app, f"{goal_id}:{app}",
                f"amplai/{safe}-{app}", earlier,
            )  # fmt: skip
        apps = list(published)
        for i, app in enumerate(apps):
            later = [u for b in apps[i + 1 :] if (u := published[b].get("pr_url"))]
            value = published[app]
            if later and value.get("pr_url") and not value.get("linked"):
                # recorded before the comment: a retry after a crash never comments twice; a
                # failed comment is recorded as not linked (the later PR's body links it anyway)
                value["linked"] = True
                self._save(f"{goal_id}:{app}", "done", value)
                try:
                    self.pr_linker(
                        self.service.apps[app].config.repo,
                        value["pr_url"],
                        f"Part of AMPLAI goal `{goal_id}` with: " + ", ".join(later),
                    )
                except Exception:
                    value["linked"] = False
                    self._save(f"{goal_id}:{app}", "done", value)
                    raise
        first = published[apps[0]]
        return {**first, "publications": published}

    @staticmethod
    def _consent(decision: dict[str, Any], app_id: str) -> dict[str, Any]:
        """The approval's publication consent for one app (per-app values win)."""
        publish: dict[str, Any] = decision["publish"]
        return {**publish, **(publish.get("apps") or {}).get(app_id, {})}

    def _publish(
        self,
        goal_id: str,
        plan: dict[str, Any],
        decision: dict[str, Any],
        node: dict[str, Any],
        app_id: str,
        record_id: str,
        branch: str,
        earlier: list[str],
    ) -> dict[str, Any]:
        app = self.service.apps[app_id].config
        base, patch = self._change(node)
        if not patch.strip():
            raise Hold("PUBLISH_EMPTY", "A verified goal with no change is not published")
        multi = record_id != goal_id
        key = digest(
            {"goal": goal_id, "patch": digest(patch.decode("latin-1")),
             **({"app": app_id} if multi else {})}
        )  # fmt: skip
        consent = self._consent(decision, app_id)
        current = self._record(record_id)
        value: dict[str, Any] = dict(current["data"]) if current else {}
        if value and value.get("effect_key") != key:
            raise Conflict("PUBLISH_KEY", "A different change was already published for this goal")
        if not value:
            commit = self._commit(app.repo, base, patch, goal_id, plan)
            value = {
                "goal_id": goal_id,
                **({"app": app_id} if multi else {}),
                "effect_key": key,
                "branch": branch,
                "remote": consent["remote"],
                "base_branch": consent["base_branch"],
                "mode": consent["mode"],
                "commit": commit,
                "prepared_at": now(),
            }
            self._save(record_id, "prepared", value)
        if (current or {}).get("state") == "done":
            return value
        self._push(app.repo, value)
        value["pushed_at"] = value.get("pushed_at") or now()
        self._save(record_id, "pushed", value)
        if value["mode"] == "draft_pr" and not value.get("pr_url"):
            draft = plan["draft"]
            body = self._body(
                plan, draft, value, node=node if multi else None, earlier=earlier, app=app_id,
                contract_objective=lambda: str(
                    self.store.get(self.scope, "goal-contract", plan["contract_ref"])["objective"]
                ),
            )  # fmt: skip
            prefix = "[AMPLAI design] " if plan.get("mode") == "design" else "[AMPLAI] "
            suffix = f" ({app_id})" if multi else ""
            title = prefix + draft["summary"][:200] + suffix
            if self.pr_creator is not None:  # a test stand-in
                url = self.pr_creator(app.repo, branch, value["base_branch"], title, body)
            elif self.git_host is not None:
                url = self.git_host.open_draft_pr(
                    app.repo, value["remote"], branch, value["base_branch"], title, body
                )
            else:
                _no_host()
            value["pr_url"] = url
        value["done_at"] = now()
        self._save(record_id, "done", value)
        return value

    def _commit(
        self, repo: Path, base: dict[str, Any], patch: bytes, goal_id: str, plan: dict[str, Any]
    ) -> str:
        git_dir = repo / ".git"
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
            for args, data in (
                (["read-tree", base["commit"]], None),
                (["apply", "--cached", "--binary", "--whitespace=nowarn", "-"], patch),
            ):
                run = _run(["git", "--git-dir", str(git_dir), *args], cwd=repo, env=env, input=data)
                if run.returncode != 0:
                    raise Hold("PUBLISH_APPLY", "Verified patch no longer applies",
                               details=run.stderr.decode()[-400:])  # fmt: skip
            tree = _run(["git", "--git-dir", str(git_dir), "write-tree"], cwd=repo, env=env)
            if tree.returncode != 0 or not tree.stdout.strip():
                raise Hold("PUBLISH_TREE", "Could not write the change tree")
        message = (
            f"[AMPLAI] {plan['draft']['summary']}\n\n"
            f"Goal: {goal_id}\nBase: {base['commit']}\n"
            f"Verified by AMPLAI ({(plan.get('composition') or {}).get('driver_id', 'codex-cli')}"
            " in the sandbox; acceptance commands on base + patch).\n"
        )
        commit = _run(
            ["git", "--git-dir", str(git_dir), "commit-tree", tree.stdout.decode().strip(),
             "-p", base["commit"], "-F", "-"],
            cwd=repo, env={**os.environ, **self._identity(repo)}, input=message.encode(),
        )  # fmt: skip
        if commit.returncode != 0:
            raise Hold(
                "PUBLISH_COMMIT", "commit-tree failed", details=commit.stderr.decode()[-400:]
            )
        return commit.stdout.decode().strip()

    @staticmethod
    def _identity(repo: Path) -> dict[str, str]:
        """The operator's configured git identity; AMPLAI when none is configured."""
        values = {}
        for key, env in (("user.name", "NAME"), ("user.email", "EMAIL")):
            run = _run(["git", "config", key], cwd=repo)
            value = run.stdout.decode().strip() if run.returncode == 0 else ""
            fallback = "AMPLAI" if env == "NAME" else "amplai@localhost"
            for role in ("AUTHOR", "COMMITTER"):
                values[f"GIT_{role}_{env}"] = value or fallback
        return values

    def _push(self, repo: Path, value: dict[str, Any]) -> None:
        """Never forced: the module reconciles a branch already at the commit and refuses one at
        another commit (``git_host`` interface v1)."""
        if self.git_host is None:
            _no_host()
        self.git_host.push_branch(repo, value["remote"], value["branch"], value["commit"])

    @staticmethod
    def _body(
        plan: dict[str, Any],
        draft: dict[str, Any],
        value: dict[str, Any],
        *,
        node: dict[str, Any] | None = None,
        earlier: list[str] | None = None,
        app: str | None = None,
        contract_objective: Callable[[], str],
    ) -> str:
        if node is not None:  # one app of a multi-app goal: its own acceptance and base
            mapping = plan.get("acceptance_map") or {}
            acceptance = "\n".join(
                f"- {mapping[ac]['statement']} (`{mapping[ac]['verifier']}`)"
                for ac in node["acceptance_ids"]
            )
            app = app or node["node_id"][len("node-") :]
            base_commit = (plan.get("base_commits") or {}).get(app, plan["base_commit"])
            part = text_bounds.full_objective(plan, node, contract_objective)
            head = f"{draft['objective']}\n\nThis PR: {part} ({app})"
        else:
            acceptance = "\n".join(
                f"- {a['statement']} (`{a['verifier']}`)" for a in draft["acceptance"]
            )
            base_commit = plan["base_commit"]
            head = draft["objective"]
        attempts = plan.get("attempts") or []
        linked = "\n**Other PRs of this goal**: " + ", ".join(earlier) + "\n" if earlier else ""
        return (
            f"{head}\n\n**Acceptance** (verified on base + patch in the sandbox):\n"
            f"{acceptance}\n\n**Base**: `{base_commit}` · **Attempts**: {len(attempts)}\n{linked}\n"
            f"Goal `{plan['goal_id']}` · commit `{value['commit']}`.\n"
            "This PR is a draft created by AMPLAI after verification; review before merging.\n"
        )

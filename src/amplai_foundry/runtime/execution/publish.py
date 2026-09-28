"""Publish a verified change as a branch and a draft PR (D-071, R8).

Preconditions, all re-checked here rather than trusted from the caller: the goal is
``verified`` with a stored goal verification, the operator approval is present, not revoked
and consents to this publication mode, and the change is the verified work output.

The commit is built with git plumbing only (temporary index: read-tree base, apply --cached,
write-tree, commit-tree), so the operator's working tree, index and current branch are never
touched and ``main`` is never written. The branch ``amplai/<goal>`` is pushed without force; a
remote branch at another commit is a conflict, not an overwrite.

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
from typing import Any

from ..contracts.identity import digest, now
from ..errors import Conflict, Hold, RuntimeFault
from .product import PORT, LocalExecutionService

KIND = "publication"


def _run(
    args: list[str], *, cwd: Path, env: dict[str, str] | None = None, input: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args, cwd=cwd, env=env, input=input, capture_output=True, timeout=300, check=False
    )


def gh_pr_creator(repo: Path, head: str, base: str, title: str, body: str) -> str:
    """Create (or find) a draft PR with the operator's authenticated ``gh``."""
    existing = _run(["gh", "pr", "list", "--head", head, "--json", "url", "--limit", "1"], cwd=repo)
    if existing.returncode == 0:
        found = json.loads(existing.stdout or b"[]")
        if found:
            url: str = found[0]["url"]
            return url
    created = _run(
        ["gh", "pr", "create", "--draft", "--head", head, "--base", base, "--title", title,
         "--body", body],
        cwd=repo,
    )  # fmt: skip
    if created.returncode != 0:
        raise Hold("PR_CREATE", "gh pr create failed", details=created.stderr.decode()[-400:])
    return created.stdout.decode().strip().splitlines()[-1]


def gh_pr_linker(repo: Path, url: str, body: str) -> None:
    """Comment on a draft PR (links the other PRs of a multi-app goal)."""
    done = _run(["gh", "pr", "comment", url, "--body", body], cwd=repo)
    if done.returncode != 0:
        raise Hold("PR_COMMENT", "gh pr comment failed", details=done.stderr.decode()[-400:])


class GitPublisher:
    def __init__(
        self,
        service: LocalExecutionService,
        *,
        pr_creator: Callable[[Path, str, str, str, str], str] = gh_pr_creator,
        pr_linker: Callable[[Path, str, str], None] = gh_pr_linker,
    ) -> None:
        self.service, self.pr_creator, self.pr_linker = service, pr_creator, pr_linker
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
        nodes = self.store.get(self.scope, "workgraph", plan["graph_ref"])["nodes"]
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", goal_id)
        if len(nodes) == 1:
            return self._publish(
                goal_id, plan, decision, nodes[0], plan["app"], goal_id, "amplai/" + safe, []
            )
        # one draft PR per app (D-081), linked both ways
        published: dict[str, dict[str, Any]] = {}
        for node in nodes:
            app = node["node_id"][len("node-") :]
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
                self.pr_linker(
                    self.service.apps[app].config.repo,
                    value["pr_url"],
                    f"Part of AMPLAI goal `{goal_id}` with: " + ", ".join(later),
                )
                value["linked"] = True
                self._save(f"{goal_id}:{app}", "done", value)
        first = published[apps[0]]
        return {**first, "publications": published}

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
        consent = {**decision["publish"], **(decision["publish"].get("apps") or {}).get(app_id, {})}
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
            body = self._body(plan, draft, value, node=node if multi else None, earlier=earlier)
            prefix = "[AMPLAI design] " if plan.get("mode") == "design" else "[AMPLAI] "
            suffix = f" ({app_id})" if multi else ""
            value["pr_url"] = self.pr_creator(
                app.repo, branch, value["base_branch"],
                prefix + draft["summary"][:200] + suffix, body,
            )  # fmt: skip
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
        ref = "refs/heads/" + value["branch"]
        remote = _run(["git", "ls-remote", value["remote"], ref], cwd=repo)
        if remote.returncode != 0:
            raise Hold(
                "PUBLISH_REMOTE", "Remote is unreachable", details=remote.stderr.decode()[-400:]
            )
        existing = remote.stdout.decode().split()
        if existing and existing[0] == value["commit"]:
            return  # reconciled: the push already happened
        if existing:
            raise Conflict(
                "PUBLISH_BRANCH_EXISTS", "Remote branch holds another commit; not forcing"
            )
        pushed = _run(["git", "push", value["remote"], f"{value['commit']}:{ref}"], cwd=repo)
        if pushed.returncode != 0:
            raise Hold("PUBLISH_PUSH", "git push failed", details=pushed.stderr.decode()[-400:])

    @staticmethod
    def _body(
        plan: dict[str, Any],
        draft: dict[str, Any],
        value: dict[str, Any],
        *,
        node: dict[str, Any] | None = None,
        earlier: list[str] | None = None,
    ) -> str:
        if node is not None:  # one app of a multi-app goal: its own acceptance and base
            mapping = plan.get("acceptance_map") or {}
            acceptance = "\n".join(
                f"- {mapping[ac]['statement']} (`{mapping[ac]['verifier']}`)"
                for ac in node["acceptance_ids"]
            )
            app = node["node_id"][len("node-") :]
            base_commit = (plan.get("base_commits") or {}).get(app, plan["base_commit"])
            head = f"{draft['objective']}\n\nThis PR: {node['objective']} ({app})"
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

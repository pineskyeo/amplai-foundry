"""Work 018 S6 — planner draft compiled into a governed contract and graph (EX-004, D-069)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.codex import AUTH, ScopedCredential
from amplai_foundry.runtime.execution.planner_codex import CodexPlanner
from amplai_foundry.sandbox.git_workspace import CHANGE_MEDIA
from rc06_rig import DRAFT, FixedPlanner, build_rig, submit


def test_plan_compiles_a_frozen_contract_and_graph_awaiting_approval(
    deployment: Any, tmp_path: Path
) -> None:
    rig = build_rig(deployment, tmp_path)
    goal = submit(rig)
    record = rig.service.plan(goal)
    d = rig.d
    assert record["status"] == "awaiting_approval"
    # the planner read the base commit, not the operator's checkout
    assert rig.planner.calls[0]["saw_base"] == "def value():\n    return 1\n"
    assert rig.planner.calls[0]["verifiers"] == {"check": "value() must return 2"}
    contract = d.store.get(d.scope, "goal-contract", record["contract_ref"])
    graph = d.store.get(d.scope, "workgraph", record["graph_ref"])
    (node,) = graph["nodes"]
    assert contract["objective"] == DRAFT["objective"]
    assert contract["budget"]["max_wall_seconds"] == 1800
    assert contract["budget"]["max_attempts"] == 3
    assert [a["verifier_ref"] for a in contract["acceptance"]] == [
        rig.service.apps["app"].verifier_refs["check"]
    ]
    assert node["strategy"] == "bounded_loop"
    assert node["produces"] == [{"name": "change", "media_type": CHANGE_MEDIA, "required": True}]
    # nothing runs before the operator approves
    assert d.store.head(d.scope, "goal", goal)["state"] != "active"


def test_open_questions_stop_before_any_contract(deployment: Any, tmp_path: Path) -> None:
    draft = {**DRAFT, "questions": ["Which module owns value()?"]}
    rig = build_rig(deployment, tmp_path, FixedPlanner(draft))
    record = rig.service.plan(submit(rig))
    assert record["status"] == "needs_answers" and record["contract_ref"] is None


def test_an_uninstalled_verifier_is_refused(deployment: Any, tmp_path: Path) -> None:
    draft = {**DRAFT, "acceptance": [{"statement": "x", "verifier": "made-up"}]}
    rig = build_rig(deployment, tmp_path, FixedPlanner(draft))
    with pytest.raises(Hold) as exc:
        rig.service.plan(submit(rig))
    assert exc.value.code == "PLANNING_VERIFIER"


class ScriptSandbox:
    """Emits a canned Codex JSONL stream; records the command it was asked to build."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def command(self, argv: list[str], workspace: Path, name: str, **kw: Any) -> list[str]:
        self.calls.append({"argv": argv, "kw": kw})
        home = Path(kw["native_home"])
        stream = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": self.reply},
                    }
                ),
                json.dumps({"type": "turn.completed", "usage": {"output_tokens": 7}}),
            ]
        )
        script = (
            "import sys, pathlib; "
            f"assert pathlib.Path({str(home / AUTH)!r}).is_file(); "
            f"sys.stdout.write({stream!r})"
        )
        return [sys.executable, "-c", script]


def planner(tmp_path: Path, reply: str) -> tuple[CodexPlanner, ScriptSandbox, Path]:
    home = tmp_path / "scoped"
    (home / ".codex").mkdir(parents=True)
    (home / AUTH).write_text('{"t": 1}')
    sandbox = ScriptSandbox(reply)
    p = CodexPlanner(sandbox, ScopedCredential(home), tmp_path / "runs", model="gpt-5.6-sol")  # type: ignore[arg-type]
    return p, sandbox, home


def test_codex_planner_reads_read_only_with_a_pinned_schema(tmp_path: Path) -> None:
    p, sandbox, home = planner(tmp_path, json.dumps(DRAFT))
    ws = tmp_path / "ws"
    ws.mkdir()
    out = p.draft("make value return 2", "app", {"check": "value() must return 2"}, ws)
    assert out["draft"] == DRAFT and out["usage"] == {"output_tokens": 7}
    argv = sandbox.calls[0]["argv"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "/amplai-input/plan-schema.json" in sandbox.calls[0]["kw"]["readonly_mounts"]
    # the credential was leased for the run and removed afterwards
    run_home = Path(sandbox.calls[0]["kw"]["native_home"])
    assert not (run_home / AUTH).exists() and (home / AUTH).exists()


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        json.dumps({**DRAFT, "acceptance": [{"statement": "x", "verifier": "made-up"}]}),
        json.dumps({**DRAFT, "acceptance": []}),
        json.dumps({k: v for k, v in DRAFT.items() if k != "risk"}),
    ],
)
def test_codex_planner_rejects_replies_outside_the_schema(tmp_path: Path, reply: str) -> None:
    p, _, _ = planner(tmp_path, reply)
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(Hold) as exc:
        p.draft("goal", "app", {"check": "desc"}, ws)
    assert exc.value.code == "PLANNER_OUTPUT"

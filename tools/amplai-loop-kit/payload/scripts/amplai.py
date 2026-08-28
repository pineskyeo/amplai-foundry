#!/usr/bin/env python3
"""CLI for the AMPLAI decision and asynchronous cross-app protocol."""
from __future__ import print_function

import argparse
import io
import json
import os
import sys

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from amplai_runtime import (  # noqa: E402
    AmplaiError, ConflictError, LockError, NotFoundError, ProjectStore,
    ValidationError, discover_project_home, ensure_dir, load_app_identity,
    read_json, reseal_object, scan_unsealed, seal, write_json_atomic,
)


def json_value(raw, field):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ValidationError("%s must be JSON: %s" % (field, exc))


def parse_lane(raw):
    if "::" not in raw:
        raise ValidationError("lane must be NAME::GOAL: %s" % raw)
    name, goal = raw.split("::", 1)
    if not name.strip() or not goal.strip():
        raise ValidationError("lane must have non-empty NAME and GOAL")
    return {"name": name.strip(), "goal": goal.strip()}


def print_value(value, as_json=True):
    if isinstance(value, str) and not as_json:
        print(value)
        return
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def app_identity(repo, app_id, project_id):
    root = os.path.abspath(repo)
    path = os.path.join(root, ".ai-team", "app.json")
    value = seal({
        "schema_version": "1.0",
        "kind": "app_identity",
        "runtime_protocol": "amplai.async-cross-app.v1",
        "project_id": project_id,
        "app_id": app_id,
    })
    write_json_atomic(path, value)
    return value


def store_from_args(args):
    home = discover_project_home(os.getcwd(), getattr(args, "project_home", None))
    return ProjectStore(home)


def cmd_project(args):
    if args.project_action in ("reseal", "set-policy"):
        return cmd_project_repair(args)
    if args.project_action == "init":
        policy = None
        policy_path = args.policy
        if not policy_path:
            candidate = os.path.join(os.getcwd(), ".ai-team", "runtime", "async-policy.json")
            if os.path.isfile(candidate):
                policy_path = candidate
        if policy_path:
            policy = read_json(os.path.abspath(os.path.expanduser(policy_path)))
            policy.pop("content_hash", None)
        store = ProjectStore.initialize(
            args.home, args.id, args.name, git_init=not args.no_git,
            policy=policy,
        )
        return store.status_summary()
    store = store_from_args(args)
    if args.project_action == "status":
        return store.status_summary()
    if args.project_action == "verify":
        return store.verify(include_local=args.include_local)
    if args.project_action == "reconcile":
        return store.reconcile()
    raise ValidationError("unknown project action")


def cmd_project_repair(args):
    """reseal/set-policy run before ProjectStore construction is possible."""
    home = discover_project_home(os.getcwd(), args.project_home)
    if args.project_action == "reseal":
        if args.all:
            broken = scan_unsealed(home)
            results = [reseal_object(home, rel, actor=args.actor) for rel in broken]
            return {"project_home": home, "resealed": results,
                    "count": len(results)}
        if not args.path:
            raise ValidationError("reseal needs --path or --all")
        return reseal_object(home, args.path, actor=args.actor)
    if args.project_action == "set-policy":
        policy = read_json(os.path.abspath(os.path.expanduser(args.source)))
        store = ProjectStore(home)
        drift = store.policy_drift(policy)
        return {"policy": store.set_policy(policy, actor=args.actor),
                "changed_keys": drift}
    raise ValidationError("unknown project action")


def cmd_app(args):
    store = store_from_args(args)
    if args.app_action == "register":
        repo = os.path.abspath(args.repo) if args.repo else None
        value = store.register_app(
            args.id,
            repo_path=repo,
            display_name=args.name,
            max_concurrency=args.max_concurrency,
            runner_type=args.runner,
            command=args.runner_command,
            runner_args=args.runner_arg,
            auto_start=args.auto_start,
            timeout_seconds=args.timeout,
            actor=args.actor,
        )
        identity = None
        if repo and not args.no_identity:
            identity = app_identity(repo, args.id, store.project["project_id"])
        return {"app": value, "identity": identity, "local_binding": store.get_local_app(args.id, False)}
    if args.app_action == "list":
        return store.list_apps()
    if args.app_action == "show":
        return {"app": store.get_app(args.id), "local_binding": store.get_local_app(args.id, False)}
    raise ValidationError("unknown app action")


def cmd_contract(args):
    store = store_from_args(args)
    if args.contract_action == "register":
        return store.register_contract(
            args.id, args.version, args.kind,
            args.producer, args.consumer, args.source_ref,
            compatibility=args.compatibility,
            status=args.status,
            verification=args.verify_command,
            actor=args.actor,
        )
    if args.contract_action == "show":
        return store.get_contract(args.ref)
    raise ValidationError("unknown contract action")


def cmd_change(args):
    store = store_from_args(args)
    if args.change_action == "create":
        return store.create_change(
            args.title, args.goal, args.source_app,
            affected_apps=args.affected_app,
            contract_refs=args.contract_ref,
            change_id=args.id,
            actor=args.actor,
        )
    if args.change_action == "activate":
        return store.activate_change(args.id, actor=args.actor)
    if args.change_action == "show":
        return store.get_change(args.id)
    if args.change_action == "list":
        return store.list_changes()
    if args.change_action == "cancel":
        return store.cancel_change(args.id, args.reason, actor=args.actor)
    raise ValidationError("unknown change action")


def lease_token(args):
    """Resolve the lease token without putting it on a command line.

    A supervised worker already has AMPLAI_LEASE_TOKEN in its environment, so
    `--lease-token` exists only for manual operation and is discouraged: argv is
    visible to every process on the host via `ps` and is captured in agent
    transcripts.
    """
    token = getattr(args, "lease_token", None) or os.environ.get("AMPLAI_LEASE_TOKEN")
    if not token:
        raise ValidationError(
            "no lease token: run under the supervisor, or export "
            "AMPLAI_LEASE_TOKEN (see `work claim --token-file`)"
        )
    return token


def cmd_work(args):
    store = store_from_args(args)
    action = args.work_action
    if action == "create":
        return store.create_work(
            args.cr, args.target_app, args.goal,
            source_app=args.source_app,
            depends_on=args.depends_on,
            acceptance=args.acceptance,
            contract_refs=args.contract_ref,
            decision_refs=args.decision_ref,
            input_evidence_refs=args.evidence_ref,
            priority=args.priority,
            max_attempts=args.max_attempts,
            work_type=args.work_type,
            actor=args.actor,
        )
    if action == "activate":
        return store.activate_work(args.id, actor=args.actor)
    if action == "show":
        return store.get_work(args.id)
    if action == "list":
        statuses = args.status or []
        return store.list_work(cr_id=args.cr, target_app=args.app, statuses=statuses)
    if action == "next-ready":
        return store.next_ready(args.app) or {}
    if action == "context":
        if args.format == "markdown":
            return store.render_handoff(args.id)
        return store.build_work_context(args.id)
    if action == "claim":
        work, token = store.claim_work(
            args.id, args.worker, lease_seconds=args.lease_seconds, actor=args.actor,
        )
        result = {"work": work, "lease_token": "<redacted>"}
        if args.token_file:
            path = os.path.abspath(os.path.expanduser(args.token_file))
            ensure_dir(os.path.dirname(path))
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w") as stream:
                stream.write(token)
            os.chmod(path, 0o600)
            result["token_file"] = path
        elif args.print_token:
            # Opt-in only: this puts the token into whatever captures stdout.
            result["lease_token"] = token
        return result
    if action == "start":
        return store.start_work(args.id, lease_token(args), actor=args.actor)
    if action == "heartbeat":
        return store.heartbeat(args.id, lease_token(args), actor=args.actor)
    if action == "complete":
        return store.complete_work(
            args.id, lease_token(args), args.summary,
            args.evidence_ref, decision_refs=args.decision_ref,
            outputs=args.output, actor=args.actor,
        )
    if action == "wait":
        return store.wait_work(
            args.id, lease_token(args), args.depends_on, args.reason, actor=args.actor,
        )
    if action == "block":
        return store.block_work(args.id, lease_token(args), args.reason, actor=args.actor)
    if action == "human-required":
        return store.require_human(
            args.id, lease_token(args), args.gate, args.reason,
            question_refs=args.question_ref, actor=args.actor,
        )
    if action == "fail":
        return store.fail_work(
            args.id, lease_token(args), args.error,
            retryable=args.retryable, actor=args.actor,
        )
    if action == "cancel":
        return store.cancel_work(
            args.id, args.reason, cascade=args.cascade, actor=args.actor,
        )
    if action == "retarget":
        return store.retarget_work(args.id, args.depends_on, actor=args.actor)
    if action == "reset-attempts":
        return store.reset_work_attempts(
            args.id, max_attempts=args.max_attempts, actor=args.actor,
        )
    raise ValidationError("unknown work action")


def cmd_evidence(args):
    store = store_from_args(args)
    if args.evidence_action == "add":
        metadata = json_value(args.metadata_json, "metadata-json") or {}
        return store.add_evidence(
            args.cr, args.type, args.summary, args.source_kind,
            args.source_locator, work_id=args.work,
            app_id=args.app, facts=args.fact,
            metadata=metadata, actor=args.actor,
        )
    if args.evidence_action == "show":
        return store.get_evidence(args.id)
    raise ValidationError("unknown evidence action")


def cmd_question(args):
    store = store_from_args(args)
    if args.question_action == "create":
        return store.create_question(
            args.work, args.question,
            decision_class=args.decision_class,
            reversibility=args.reversibility,
            blast_radius=args.blast_radius,
            requested_authority=args.authority,
            evidence_mode=args.evidence_mode,
            evidence_refs=args.evidence_ref,
            evidence_lanes=[parse_lane(item) for item in args.lane],
            alternatives=args.alternative,
            actor=args.actor,
        )
    if args.question_action == "defer":
        return store.defer_question(args.id, args.review_trigger, actor=args.actor)
    if args.question_action == "show":
        return store.get_question(args.id)
    if args.question_action == "list":
        works = store.list_work(cr_id=args.cr)
        result = []
        for work in works:
            for ref in work.get("question_refs") or []:
                question = store.get_question(ref)
                if args.status and question.get("status") not in args.status:
                    continue
                result.append(question)
        return result
    raise ValidationError("unknown question action")


def cmd_decision(args):
    store = store_from_args(args)
    if args.decision_action == "record":
        return store.record_decision(
            args.question, args.statement, args.rationale,
            args.evidence_ref,
            authority=args.authority,
            alternatives=args.alternative,
            review_evidence_refs=args.review_evidence_ref,
            approval_evidence_refs=args.approval_evidence_ref,
            review_trigger=args.review_trigger,
            supersedes=args.supersedes,
            actor=args.actor,
        )
    if args.decision_action == "show":
        return store.get_decision(args.id)
    if args.decision_action == "list":
        changes = [store.get_change(args.cr)] if args.cr else store.list_changes()
        result = []
        for change in changes:
            for ref in change.get("decision_refs") or []:
                decision = store.get_decision(ref)
                if args.status and decision.get("status") not in args.status:
                    continue
                result.append(decision)
        return result
    raise ValidationError("unknown decision action")


def cmd_handoff(args):
    store = store_from_args(args)
    text = store.render_handoff(args.work)
    if args.output:
        with io.open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
        return {"written": os.path.abspath(args.output), "work_id": args.work}
    return text


def add_global(parser):
    parser.add_argument(
        "--project-home",
        help="Central Project Store. Defaults to AMPLAI_PROJECT_HOME.",
    )
    parser.add_argument("--json", action="store_true", help="Always print JSON output")


def add_actor(parser, default="agent"):
    parser.add_argument("--actor", default=default)


def build_parser():
    parser = argparse.ArgumentParser(
        description="AMPLAI Decision and Async Cross-App Runtime",
    )
    add_global(parser)
    sub = parser.add_subparsers(dest="command")

    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_action")
    pinit = project_sub.add_parser("init")
    pinit.add_argument("--home", required=True)
    pinit.add_argument("--id", required=True)
    pinit.add_argument("--name")
    pinit.add_argument(
        "--policy",
        help="Policy JSON. Defaults to .ai-team/runtime/async-policy.json when present.",
    )
    pinit.add_argument("--no-git", action="store_true")
    project_sub.add_parser("status")
    pverify = project_sub.add_parser("verify")
    pverify.add_argument("--include-local", action="store_true")
    project_sub.add_parser("reconcile")
    preseal = project_sub.add_parser(
        "reseal", help="re-record content_hash after an approved manual edit")
    preseal.add_argument("--path", help="store-relative path, e.g. changes/CR-0001/change.json")
    preseal.add_argument("--all", action="store_true", help="reseal every mismatched object")
    ppolicy = project_sub.add_parser(
        "set-policy", help="replace the central policy from a JSON file")
    ppolicy.add_argument("--from", dest="source", required=True)

    app = sub.add_parser("app")
    app_sub = app.add_subparsers(dest="app_action")
    areg = app_sub.add_parser("register")
    areg.add_argument("--id", required=True)
    areg.add_argument("--repo")
    areg.add_argument("--name")
    areg.add_argument("--max-concurrency", type=int, default=1)
    areg.add_argument("--runner", default="claude-code", choices=["claude-code", "command"])
    areg.add_argument("--command", dest="runner_command", default="claude")
    areg.add_argument("--runner-arg", action="append", default=[])
    areg.add_argument("--auto-start", action="store_true")
    areg.add_argument("--timeout", type=int)
    areg.add_argument("--no-identity", action="store_true")
    add_actor(areg, "human")
    app_sub.add_parser("list")
    ashow = app_sub.add_parser("show")
    ashow.add_argument("--id", required=True)

    contract = sub.add_parser("contract")
    contract_sub = contract.add_subparsers(dest="contract_action")
    creg = contract_sub.add_parser("register")
    creg.add_argument("--id", required=True)
    creg.add_argument("--version", required=True)
    creg.add_argument("--kind", required=True)
    creg.add_argument("--producer", action="append", required=True)
    creg.add_argument("--consumer", action="append", required=True)
    creg.add_argument("--source-ref", required=True)
    creg.add_argument("--compatibility", default="BACKWARD")
    creg.add_argument("--status", default="ACTIVE")
    creg.add_argument("--verify-command", action="append", default=[])
    add_actor(creg, "human")
    cshow = contract_sub.add_parser("show")
    cshow.add_argument("--ref", required=True)

    change = sub.add_parser("change")
    change_sub = change.add_subparsers(dest="change_action")
    ccreate = change_sub.add_parser("create")
    ccreate.add_argument("--id")
    ccreate.add_argument("--title", required=True)
    ccreate.add_argument("--goal", required=True)
    ccreate.add_argument("--source-app", required=True)
    ccreate.add_argument("--affected-app", action="append", default=[])
    ccreate.add_argument("--contract-ref", action="append", default=[])
    add_actor(ccreate, "human")
    cactivate = change_sub.add_parser("activate")
    cactivate.add_argument("--id", required=True)
    add_actor(cactivate, "human")
    ccancel = change_sub.add_parser("cancel")
    ccancel.add_argument("--id", required=True)
    ccancel.add_argument("--reason", required=True)

    cshow2 = change_sub.add_parser("show")
    cshow2.add_argument("--id", required=True)
    change_sub.add_parser("list")

    work = sub.add_parser("work")
    work_sub = work.add_subparsers(dest="work_action")
    wcreate = work_sub.add_parser("create")
    wcreate.add_argument("--cr", required=True)
    wcreate.add_argument("--target-app", required=True)
    wcreate.add_argument("--source-app")
    wcreate.add_argument("--goal", required=True)
    wcreate.add_argument("--depends-on", action="append", default=[])
    wcreate.add_argument("--acceptance", action="append", required=True)
    wcreate.add_argument("--contract-ref", action="append", default=[])
    wcreate.add_argument("--decision-ref", action="append", default=[])
    wcreate.add_argument("--evidence-ref", action="append", default=[])
    wcreate.add_argument("--priority", type=int, default=50)
    wcreate.add_argument("--max-attempts", type=int)
    wcreate.add_argument("--work-type", default="implementation")
    add_actor(wcreate)
    wactivate = work_sub.add_parser("activate")
    wactivate.add_argument("--id", required=True)
    add_actor(wactivate, "human")
    wcancel = work_sub.add_parser(
        "cancel", help="abandon a Work so its dependents stop waiting on it")
    wcancel.add_argument("--id", required=True)
    wcancel.add_argument("--reason", required=True)
    wcancel.add_argument("--cascade", action="store_true",
                         help="also cancel Work that depends on it")

    wretarget = work_sub.add_parser(
        "retarget", help="replace this Work's dependency set")
    wretarget.add_argument("--id", required=True)
    wretarget.add_argument("--depends-on", action="append", default=[])

    wreset = work_sub.add_parser(
        "reset-attempts", help="give an exhausted Work a fresh attempt budget")
    wreset.add_argument("--id", required=True)
    wreset.add_argument("--max-attempts", type=int)

    wshow = work_sub.add_parser("show")
    wshow.add_argument("--id", required=True)
    wlist = work_sub.add_parser("list")
    wlist.add_argument("--cr")
    wlist.add_argument("--app")
    wlist.add_argument("--status", action="append", default=[])
    wnext = work_sub.add_parser("next-ready")
    wnext.add_argument("--app")
    wcontext = work_sub.add_parser("context")
    wcontext.add_argument("--id", required=True)
    wcontext.add_argument("--format", choices=["json", "markdown"], default="json")
    wclaim = work_sub.add_parser("claim")
    wclaim.add_argument("--id", required=True)
    wclaim.add_argument("--worker", required=True)
    wclaim.add_argument("--lease-seconds", type=int)
    wclaim.add_argument("--token-file", help="write the lease token to a 0600 file")
    wclaim.add_argument("--print-token", action="store_true",
                        help="print the lease token to stdout (not recommended)")
    add_actor(wclaim, "supervisor")
    wstart = work_sub.add_parser("start")
    wstart.add_argument("--id", required=True)
    wstart.add_argument("--lease-token", help="discouraged; prefer AMPLAI_LEASE_TOKEN")
    add_actor(wstart, "worker")
    wheart = work_sub.add_parser("heartbeat")
    wheart.add_argument("--id", required=True)
    wheart.add_argument("--lease-token", help="discouraged; prefer AMPLAI_LEASE_TOKEN")
    add_actor(wheart, "supervisor")
    wcomplete = work_sub.add_parser("complete")
    wcomplete.add_argument("--id", required=True)
    wcomplete.add_argument("--lease-token", help="discouraged; prefer AMPLAI_LEASE_TOKEN")
    wcomplete.add_argument("--summary", required=True)
    wcomplete.add_argument("--evidence-ref", action="append", required=True)
    wcomplete.add_argument("--decision-ref", action="append", default=[])
    wcomplete.add_argument("--output", action="append", default=[])
    add_actor(wcomplete, "worker")
    wwait = work_sub.add_parser("wait")
    wwait.add_argument("--id", required=True)
    wwait.add_argument("--lease-token", help="discouraged; prefer AMPLAI_LEASE_TOKEN")
    wwait.add_argument("--depends-on", action="append", required=True)
    wwait.add_argument("--reason", required=True)
    add_actor(wwait, "worker")
    wblock = work_sub.add_parser("block")
    wblock.add_argument("--id", required=True)
    wblock.add_argument("--lease-token", help="discouraged; prefer AMPLAI_LEASE_TOKEN")
    wblock.add_argument("--reason", required=True)
    add_actor(wblock, "worker")
    whuman = work_sub.add_parser("human-required")
    whuman.add_argument("--id", required=True)
    whuman.add_argument("--lease-token", help="discouraged; prefer AMPLAI_LEASE_TOKEN")
    whuman.add_argument("--gate", required=True)
    whuman.add_argument("--reason", required=True)
    whuman.add_argument("--question-ref", action="append", default=[])
    add_actor(whuman, "worker")
    wfail = work_sub.add_parser("fail")
    wfail.add_argument("--id", required=True)
    wfail.add_argument("--lease-token", help="discouraged; prefer AMPLAI_LEASE_TOKEN")
    wfail.add_argument("--error", required=True)
    wfail.add_argument("--retryable", action="store_true")
    add_actor(wfail, "worker")

    evidence = sub.add_parser("evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_action")
    eadd = evidence_sub.add_parser("add")
    eadd.add_argument("--cr", required=True)
    eadd.add_argument("--work")
    eadd.add_argument("--app")
    eadd.add_argument("--type", required=True)
    eadd.add_argument("--summary", required=True)
    eadd.add_argument("--source-kind", required=True)
    eadd.add_argument("--source-locator", required=True)
    eadd.add_argument("--fact", action="append", default=[])
    eadd.add_argument("--metadata-json")
    add_actor(eadd)
    eshow = evidence_sub.add_parser("show")
    eshow.add_argument("--id", required=True)

    question = sub.add_parser("question")
    question_sub = question.add_subparsers(dest="question_action")
    qcreate = question_sub.add_parser("create")
    qcreate.add_argument("--work", required=True)
    qcreate.add_argument("--question", required=True)
    qcreate.add_argument("--decision-class", default="engineering")
    qcreate.add_argument("--reversibility", default="high")
    qcreate.add_argument("--blast-radius", default="low")
    qcreate.add_argument("--authority")
    qcreate.add_argument("--evidence-mode")
    qcreate.add_argument("--evidence-ref", action="append", default=[])
    qcreate.add_argument("--lane", action="append", default=[])
    qcreate.add_argument("--alternative", action="append", default=[])
    add_actor(qcreate)
    qdefer = question_sub.add_parser("defer")
    qdefer.add_argument("--id", required=True)
    qdefer.add_argument("--review-trigger", required=True)
    add_actor(qdefer)
    qshow = question_sub.add_parser("show")
    qshow.add_argument("--id", required=True)
    qlist = question_sub.add_parser("list")
    qlist.add_argument("--cr")
    qlist.add_argument("--status", action="append", default=[])

    decision = sub.add_parser("decision")
    decision_sub = decision.add_subparsers(dest="decision_action")
    drecord = decision_sub.add_parser("record")
    drecord.add_argument("--question", required=True)
    drecord.add_argument("--statement", required=True)
    drecord.add_argument("--rationale", required=True)
    drecord.add_argument("--authority")
    drecord.add_argument("--evidence-ref", action="append", required=True)
    drecord.add_argument("--review-evidence-ref", action="append", default=[])
    drecord.add_argument("--approval-evidence-ref", action="append", default=[])
    drecord.add_argument("--alternative", action="append", default=[])
    drecord.add_argument("--review-trigger")
    drecord.add_argument("--supersedes")
    add_actor(drecord)
    dshow = decision_sub.add_parser("show")
    dshow.add_argument("--id", required=True)
    dlist = decision_sub.add_parser("list")
    dlist.add_argument("--cr")
    dlist.add_argument("--status", action="append", default=[])

    handoff = sub.add_parser("handoff")
    handoff_sub = handoff.add_subparsers(dest="handoff_action")
    hrender = handoff_sub.add_parser("render")
    hrender.add_argument("--work", required=True)
    hrender.add_argument("--output")

    return parser


def dispatch(args):
    if args.command == "project":
        return cmd_project(args)
    if args.command == "app":
        return cmd_app(args)
    if args.command == "contract":
        return cmd_contract(args)
    if args.command == "change":
        return cmd_change(args)
    if args.command == "work":
        return cmd_work(args)
    if args.command == "evidence":
        return cmd_evidence(args)
    if args.command == "question":
        return cmd_question(args)
    if args.command == "decision":
        return cmd_decision(args)
    if args.command == "handoff" and args.handoff_action == "render":
        return cmd_handoff(args)
    raise ValidationError("a command and action are required")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        value = dispatch(args)
        as_json = args.json or not isinstance(value, str)
        print_value(value, as_json=as_json)
        if isinstance(value, dict) and value.get("ok") is False:
            return 1
        return 0
    except NotFoundError as exc:
        print("NOT_FOUND: %s" % exc, file=sys.stderr)
        return 4
    except ConflictError as exc:
        print("CONFLICT: %s" % exc, file=sys.stderr)
        return 3
    except LockError as exc:
        print("LOCK_ERROR: %s" % exc, file=sys.stderr)
        return 5
    except (ValidationError, AmplaiError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

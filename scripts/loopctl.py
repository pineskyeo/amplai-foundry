#!/usr/bin/env python3
"""Small control-plane utilities for the AMPLAI Loop Runtime.

This is intentionally not an orchestration framework. It only makes policy,
contract, and runtime structure deterministic enough for the work/design entry points.

Commands:
  doctor
  classify [--staged|--working] [PATH ...]
  contract validate PATH
  readiness init|evaluate|validate
  discovery scan
  context build|validate
  environment capture
  permission check
  trace append|verify
  handoff write
  quadrant audit
  docs impact|validate [--repo]
  garden scan|incremental|full|apply
  status [FEATURE_DIR]
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import loopv2

POLICY = os.path.join(".ai-team", "runtime", "policy.json")
REGISTRY = os.path.join(".ai-team", "verifiers", "registry.json")
CONTRACT_SCHEMA = os.path.join(".ai-team", "contracts", "work-contract.schema.json")

# verifier profile 이름의 정본은 REGISTRY 의 profiles 다. 아래는 policy 에 profile_rank 가
# 없을 때만 쓰는 fallback 이고, doctor 가 정본과 어긋나면 block 한다.
PROFILE_RANK_FALLBACK = {
    "fast": 0,
    "commit": 1,
    "standard": 2,
    "runtime": 3,
    "full": 4,
    "v2": 5,
}


def root_dir():
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.STDOUT
        )
        return out.decode("utf-8", "replace").strip()
    except Exception:
        return os.getcwd()


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def run_git(args):
    try:
        out = subprocess.check_output(["git", *list(args)], stderr=subprocess.STDOUT)
        return out.decode("utf-8", "replace")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.output.decode("utf-8", "replace"))


def normalize_path(path):
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def working_paths(staged=False):
    if staged:
        text = run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMRD"])
        return [normalize_path(line) for line in text.splitlines() if line.strip()]
    text = run_git(["status", "--porcelain=v1", "--untracked-files=all"])
    result = []
    for line in text.splitlines():
        if len(line) < 4:
            continue
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        result.append(normalize_path(value))
    return result


def match_path(path, pattern):
    path = normalize_path(path)
    pattern = normalize_path(pattern)
    return fnmatch.fnmatchcase(path, pattern)


def classify_paths(policy, paths):
    paths = [normalize_path(p) for p in paths if p and p.strip()]
    risk_rank = policy.get("risk_rank") or {"low": 0, "normal": 1, "high": 2}
    # module 상수를 그대로 넘기지 않는다. 호출자가 반환값을 고쳐도 상수가 오염되지 않는다.
    profile_rank = policy.get("profile_rank") or dict(PROFILE_RANK_FALLBACK)
    default_risk = (policy.get("defaults") or {}).get("risk", "normal")
    default_profile = (policy.get("defaults") or {}).get("profile", "standard")
    risk = "low" if paths else default_risk
    profile = "fast" if paths else default_profile
    gates = []
    matched = []
    matched_paths = set()

    for path in paths:
        for rule in policy.get("path_rules") or []:
            patterns = rule.get("patterns") or []
            if not any(match_path(path, pattern) for pattern in patterns):
                continue
            matched_paths.add(path)
            matched.append({"path": path, "rule": rule.get("id")})
            candidate_risk = rule.get("risk", risk)
            candidate_profile = rule.get("profile", profile)
            if risk_rank.get(candidate_risk, -1) > risk_rank.get(risk, -1):
                risk = candidate_risk
            if profile_rank.get(candidate_profile, -1) > profile_rank.get(profile, -1):
                profile = candidate_profile
            for gate in rule.get("gates") or []:
                if gate not in gates:
                    gates.append(gate)

    unmatched = [path for path in paths if path not in matched_paths]
    if unmatched:
        if risk_rank.get(default_risk, -1) > risk_rank.get(risk, -1):
            risk = default_risk
        if profile_rank.get(default_profile, -1) > profile_rank.get(profile, -1):
            profile = default_profile
        for path in unmatched:
            matched.append({"path": path, "rule": "default-unmatched"})

    minimum = (policy.get("risk_profiles") or {}).get(risk)
    if minimum and profile_rank.get(minimum, -1) > profile_rank.get(profile, -1):
        profile = minimum

    return {
        "schema_version": "2.0",
        "paths": paths,
        "risk": risk,
        "profile": profile,
        "human_gates": gates,
        "matched": matched,
    }


def print_classification(result, as_json=False):
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("risk    : {}".format(result["risk"]))
    print("profile : {}".format(result["profile"]))
    gates = result.get("human_gates") or []
    print("gates   : %s" % (", ".join(gates) if gates else "none"))
    print("paths   : %d" % len(result.get("paths") or []))
    for item in result.get("matched") or []:
        print("  %-48s <- %s" % (item["path"], item["rule"]))


def validate_contract(path, policy, registry):
    errors = []
    try:
        value = load_json(path)
    except Exception as exc:
        return [f"JSON을 읽을 수 없음: {exc}"]

    required = [
        "schema_version",
        "id",
        "goal",
        "risk",
        "scope",
        "acceptance",
        "verification",
        "retry",
        "status",
    ]
    for key in required:
        if key not in value:
            errors.append(f"필수 필드 없음: {key}")

    schema_version = value.get("schema_version")
    if schema_version not in ("1.0", "2.0"):
        errors.append("schema_version은 1.0 또는 2.0이어야 함")
    if not isinstance(value.get("id"), str) or not value.get("id", "").strip():
        errors.append("id는 비어 있지 않은 문자열이어야 함")
    if not isinstance(value.get("goal"), str) or not value.get("goal", "").strip():
        errors.append("goal은 비어 있지 않은 문자열이어야 함")

    risk = value.get("risk")
    if risk not in ("low", "normal", "high"):
        errors.append("risk는 low|normal|high 중 하나여야 함")

    scope = value.get("scope")
    if not isinstance(scope, dict):
        errors.append("scope는 object여야 함")
    else:
        for key in ("include", "exclude"):
            if not isinstance(scope.get(key), list):
                errors.append(f"scope.{key}는 array여야 함")
        if risk in ("normal", "high") and not (scope.get("include") or []):
            errors.append("normal/high contract는 scope.include가 비어 있으면 안 됨")

    acceptance = value.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        errors.append("acceptance는 1개 이상이어야 함")
    else:
        ids = set()
        for idx, item in enumerate(acceptance, start=1):
            if not isinstance(item, dict):
                errors.append("acceptance[%d]는 object여야 함" % idx)
                continue
            aid = item.get("id")
            statement = item.get("statement")
            if not isinstance(aid, str) or not aid.strip():
                errors.append("acceptance[%d].id가 비어 있음" % idx)
            elif aid in ids:
                errors.append(f"acceptance id 중복: {aid}")
            else:
                ids.add(aid)
            if not isinstance(statement, str) or not statement.strip():
                errors.append("acceptance[%d].statement가 비어 있음" % idx)
            evidence = item.get("evidence", [])
            if not isinstance(evidence, list):
                errors.append("acceptance[%d].evidence는 array여야 함" % idx)

    profiles = (registry.get("profiles") or {}).keys()
    verification = value.get("verification")
    profile = None
    if not isinstance(verification, dict):
        errors.append("verification은 object여야 함")
    else:
        profile = verification.get("profile")
        if profile not in profiles:
            errors.append(f"등록되지 않은 verifier profile: {profile}")

    if risk in ("low", "normal", "high") and profile:
        ranks = policy.get("profile_rank") or {}
        minimum = (policy.get("risk_profiles") or {}).get(risk)
        if minimum and ranks.get(profile, -1) < ranks.get(minimum, -1):
            errors.append(f"risk {risk}의 최소 profile은 {minimum}인데 {profile}가 지정됨")

    retry = value.get("retry")
    if not isinstance(retry, dict):
        errors.append("retry는 object여야 함")
    else:
        attempts = retry.get("max_attempts_per_slice")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 10:
            errors.append("retry.max_attempts_per_slice는 1~10 정수여야 함")

    statuses = (
        "draft",
        "discovering",
        "awaiting_approval",
        "ready",
        "executing",
        "blocked",
        "done",
    )
    if value.get("status") not in statuses:
        errors.append("status가 허용값이 아님")

    known_gates = set((policy.get("human_gates") or {}).keys())
    gates = value.get("human_gates", [])
    if not isinstance(gates, list):
        errors.append("human_gates는 array여야 함")
    else:
        for gate in gates:
            if gate not in known_gates:
                errors.append(f"알 수 없는 human gate: {gate}")

    if schema_version == "2.0":
        work_type = value.get("work_type")
        allowed_work_types = (
            "tiny_change",
            "bug_fix",
            "logic_change",
            "refactor",
            "new_feature",
            "domain_heavy",
            "architecture",
            "operations",
            "repository_gardening",
        )
        if work_type not in allowed_work_types:
            errors.append("V2 work_type이 허용값이 아님")
        knowledge = value.get("knowledge")
        if not isinstance(knowledge, dict):
            errors.append("V2 contract는 knowledge object가 필요함")
        else:
            if knowledge.get("readiness") not in ("bypass", "optional", "required"):
                errors.append("knowledge.readiness가 허용값이 아님")
            if knowledge.get("context_pack") not in ("optional", "required"):
                errors.append("knowledge.context_pack이 허용값이 아님")
            if knowledge.get("domain_discovery") not in ("optional", "required_if_not_ready"):
                errors.append("knowledge.domain_discovery가 허용값이 아님")
        environment = value.get("environment")
        if not isinstance(environment, dict) or environment.get("fingerprint") not in (
            "optional",
            "required",
        ):
            errors.append("V2 contract는 environment.fingerprint가 필요함")
        provenance = value.get("provenance")
        if not isinstance(provenance, dict) or provenance.get("evidence_trace") not in (
            "optional",
            "required",
        ):
            errors.append("V2 contract는 provenance.evidence_trace가 필요함")
        if (
            work_type in ("domain_heavy", "architecture", "operations", "repository_gardening")
            or risk == "high"
        ):
            if isinstance(knowledge, dict) and knowledge.get("readiness") != "required":
                errors.append("domain-heavy/high V2 contract는 Knowledge Readiness required")
            if isinstance(knowledge, dict) and knowledge.get("context_pack") != "required":
                errors.append("domain-heavy/high V2 contract는 Context Pack required")

    return errors


def read_frontmatter(path):
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except Exception:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    data = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line or line.startswith(" "):
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip("\"'")
    return data


def codex_implicit_invocation_disabled(path):
    if not os.path.isfile(path):
        return False
    text = open(path, encoding="utf-8").read()
    return bool(
        re.search(r"^\s*allow_implicit_invocation:\s*false\s*$", text, re.MULTILINE)
    )


def hook_commands(settings, event):
    commands = []
    for wrapper in ((settings.get("hooks") or {}).get(event) or []):
        for hook in wrapper.get("hooks") or []:
            if hook.get("type") == "command" and hook.get("command"):
                commands.append(hook["command"])
    return commands


def doctor(root):
    required = [
        ".ai-team/README.md",
        ".ai-team/runtime/WORKFLOW.md",
        ".ai-team/runtime/policy.json",
        ".ai-team/runtime/escalation.md",
        ".ai-team/contracts/README.md",
        ".ai-team/contracts/work-contract.schema.json",
        ".ai-team/contracts/work-contract.template.json",
        ".ai-team/verifiers/README.md",
        ".ai-team/verifiers/registry.json",
        ".ai-team/verifiers/run.py",
        ".ai-team/policy/knowledge-readiness.json",
        ".ai-team/policy/permissions.json",
        ".ai-team/policy/tdd.json",
        ".ai-team/policy/quadrant.json",
        ".ai-team/policy/documentation.json",
        ".ai-team/policy/gardening.json",
        ".ai-team/knowledge/map.json",
        ".ai-team/knowledge/claims.jsonl",
        ".ai-team/knowledge/context-pack.schema.json",
        ".ai-team/knowledge/doc-impact.schema.json",
        ".ai-team/knowledge/garden-report.schema.json",
        ".ai-team/evidence/provenance.schema.json",
        # semantic runtime(ontology/SHACL/MCP/project miner)은 amplai-foundry 로
        # 이식하지 않았다 (D-046). 이 저장소는 Knowledge Vault 와 Proposal 모델이
        # 그 자리를 대신한다. required 에서 뺀다.
        "scripts/loopv2.py",
        ".agents/skills/work/SKILL.md",
        ".agents/skills/design/SKILL.md",
        ".agents/skills/dev-loop/SKILL.md",
        "scripts/eval.sh",
        "scripts/loopctl.py",
    ]
    forbidden = [
        ".ai-team/agents",
        ".ai-team/analytics",
        ".ai-team/config",
        ".ai-team/docs",
        ".ai-team/evals",
        ".ai-team/examples",
        ".ai-team/feedback",
        ".ai-team/gates",
        ".ai-team/hooks",
        ".ai-team/improvements",
        ".ai-team/monitor",
        ".ai-team/project",
        ".ai-team/registry",
        ".ai-team/skills",
        ".ai-team/templates",
        ".ai-team/workflows",
        ".ai-team/wrappers",
        ".claude/agents",
        ".codex/agents",
        # `.specify/workflows` 는 amplai-foundry 에서 아직 살아 있다 — `workflow.yml` 의
        # `review-implementation` step 이 three-lens gate 의 근거이고 AGENTS.md 와
        # CLAUDE.md 가 그것을 인용한다. `/work` 가 그 관문을 흡수하면 그때 지운다.
        ".opencode/plugins",
    ]
    # amplai-foundry 는 cortex 보다 skill 이 많다. 이식하지 않은 것들은 그대로 둔다 —
    # `eli12`/`feynman`/`grilling` 은 설명·학습·심문용이고 loop 와 겹치지 않는다.
    # `speckit-checklist`/`speckit-constitution`/`speckit-taskstoissues` 도 유지한다.
    expected_skills = {
        "work",
        "design",
        "dev-loop",
        "speckit-specify",
        "speckit-clarify",
        "speckit-plan",
        "speckit-analyze",
        "taskify",
        "speckit-implement",
        "speckit-converge",
        "code-review",
        "systematic-debugging",
        "eli12",
        "feynman",
        "grill-me",
        "grilling",
        "speckit-checklist",
        "speckit-constitution",
        "speckit-taskstoissues",
    }
    errors = []

    for rel in required:
        if not os.path.exists(os.path.join(root, rel)):
            errors.append(f"required path 없음: {rel}")
    for rel in forbidden:
        if os.path.exists(os.path.join(root, rel)):
            errors.append(f"legacy path가 남아 있음: {rel}")

    policy = None
    registry = None
    contract_schema = None
    for rel in (POLICY, REGISTRY, CONTRACT_SCHEMA):
        try:
            value = load_json(os.path.join(root, rel))
            if rel == POLICY:
                policy = value
            elif rel == REGISTRY:
                registry = value
            else:
                contract_schema = value
        except Exception as exc:
            errors.append(f"invalid JSON {rel}: {exc}")

    if policy is not None:
        if policy.get("runtime") != "amplai-loop-v2":
            errors.append("runtime policy는 amplai-loop-v2여야 함")
        if sorted(policy.get("public_commands") or []) != ["design", "work"]:
            errors.append("policy public_commands는 work/design만이어야 함")
        for rule in policy.get("path_rules") or []:
            rule_profile = rule.get("profile")
            if rule_profile and rule_profile not in (policy.get("profile_rank") or {}):
                errors.append(
                    "path_rule {}이 profile_rank에 없는 {}를 가리킴".format(
                        rule.get("id"), rule_profile
                    )
                )
        for gate in policy.get("human_gates") or {}:
            if not isinstance(gate, str) or not gate:
                errors.append("human gate id가 올바르지 않음")

    shared_root = os.path.join(root, ".agents", "skills")
    shared_names = set()
    public = []
    internal = []
    internal_wrong = []
    if not os.path.isdir(shared_root):
        errors.append(".agents/skills 없음")
    else:
        for name in sorted(os.listdir(shared_root)):
            skill = os.path.join(shared_root, name, "SKILL.md")
            if not os.path.isfile(skill):
                continue
            shared_names.add(name)
            fm = read_frontmatter(skill)
            raw = fm.get("user-invocable", "true").lower()
            if raw == "true":
                public.append(name)
            elif raw == "false":
                internal.append(name)
            else:
                internal_wrong.append(f"{name} user-invocable={raw}")
    if shared_names != expected_skills:
        missing = sorted(expected_skills - shared_names)
        extra = sorted(shared_names - expected_skills)
        if missing:
            errors.append("필수 shared skill 없음: {}".format(", ".join(missing)))
        if extra:
            errors.append("허용되지 않은/legacy shared skill: {}".format(", ".join(extra)))

    # 개발 loop의 공개 표면은 work/design 둘이고 나머지 public skill은 loop 밖 보조 기능이다.
    allowed_public = {"work", "design", "eli12", "feynman", "grill-me", "grilling"}
    stray = sorted(set(public) - allowed_public)
    if stray:
        errors.append(
            "개발 skill 은 work/design 만 user-invocable 이어야 함: {}".format(
                ", ".join(stray)
            )
        )
    if internal_wrong:
        errors.extend(f"invalid skill visibility: {x}" for x in internal_wrong)

    # Codex는 `.agents/skills`를 직접 읽는다. 내부 capability가 prompt만으로 암묵 호출되면
    # public work/design controller를 우회하므로 invocation policy도 doctor가 강제한다.
    for name in internal:
        metadata = os.path.join(shared_root, name, "agents", "openai.yaml")
        if not codex_implicit_invocation_disabled(metadata):
            errors.append(
                "Codex internal skill invocation policy 없음/오류: "
                f".agents/skills/{name}/agents/openai.yaml"
            )

    # `.agents/skills`가 유일한 workflow 정본이고 Claude는 exact symlink mirror다.
    claude_root = os.path.join(root, ".claude", "skills")
    claude_names = set()
    if os.path.isdir(claude_root):
        for name in sorted(os.listdir(claude_root)):
            path = os.path.join(claude_root, name)
            claude_names.add(name)
            expected_target = os.path.join("..", "..", ".agents", "skills", name)
            if not os.path.islink(path):
                errors.append(
                    f"Claude skill은 shared 정본의 symlink여야 함: .claude/skills/{name}"
                )
                continue
            actual_target = os.readlink(path)
            if actual_target != expected_target:
                errors.append(
                    "Claude skill symlink 방향 오류: .claude/skills/{} -> {} "
                    "(expected {})".format(name, actual_target, expected_target)
                )
            elif not os.path.exists(path):
                errors.append(f"Claude skill symlink 가 깨졌다: .claude/skills/{name}")
    else:
        errors.append(".claude/skills 없음")
    if claude_names != shared_names:
        missing = sorted(shared_names - claude_names)
        extra = sorted(claude_names - shared_names)
        if missing:
            errors.append("Claude adapter symlink 없음: {}".format(", ".join(missing)))
        if extra:
            errors.append("Claude adapter에 legacy skill 남음: {}".format(", ".join(extra)))

    # Kit 2.3.2+가 설치된 repository는 Codex lifecycle hook도 설치 record와 함께 가져야 한다.
    install_state_path = os.path.join(
        root, ".ai-team", "install", "amplai-loop-kit.json"
    )
    if os.path.isfile(install_state_path):
        try:
            install_state = load_json(install_state_path)
        except Exception as exc:
            errors.append(f"invalid JSON {install_state_path}: {exc}")
        else:
            codex_hooks = install_state.get("codex_hooks") or []
            if codex_hooks:
                hooks_path = os.path.join(root, ".codex", "hooks.json")
                try:
                    settings = load_json(hooks_path)
                except Exception as exc:
                    errors.append(f"Codex hook 설정 없음/오류: {hooks_path}: {exc}")
                else:
                    for item in codex_hooks:
                        event = item.get("event")
                        command = item.get("command")
                        if command not in hook_commands(settings, event):
                            errors.append(
                                "설치 record의 Codex hook 없음: {} -> {}".format(
                                    event, command
                                )
                            )

    if registry is not None:
        check_ids = []
        for item in registry.get("checks") or []:
            check_id = item.get("id")
            if not check_id:
                errors.append("verifier check id 없음")
                continue
            check_ids.append(check_id)
            source = item.get("source")
            if (
                source
                and source.startswith(".ai-team/")
                and not os.path.exists(os.path.join(root, source))
            ):
                errors.append(f"verifier source 없음: {check_id} -> {source}")
        if len(check_ids) != len(set(check_ids)):
            errors.append("verifier check id가 중복됨")
        known = set(check_ids)
        profiles = registry.get("profiles") or {}
        for profile_name, profile in profiles.items():
            for parent in profile.get("extends") or []:
                if parent not in profiles:
                    errors.append(f"verifier profile parent 없음: {profile_name} -> {parent}")
            for check_id in profile.get("checks") or []:
                if check_id not in known:
                    errors.append(f"verifier profile check 없음: {profile_name} -> {check_id}")

        # profile 이름의 정본은 registry 다. policy·contract schema·loopctl fallback 셋이
        # 그것과 어긋나면 classify가 존재하지 않는 profile을 고르거나 실재하는 profile을
        # rank -1로 밀어낸다. 이식 잔재가 조용히 지나간 자리라 block으로 잡는다.
        #
        # 값을 못 읽으면 빈 집합으로 둔다. None으로 두고 건너뛰면 mirror 하나가 통째로
        # 빠진 채 PASS가 나고 요약줄이 "일치"라고 거짓 보고한다.
        canonical = set(profiles)
        enum = None
        if contract_schema is not None:
            enum = (
                ((contract_schema.get("properties") or {}).get("verification") or {})
                .get("properties", {})
                .get("profile", {})
                .get("enum")
            )
        schema_names = set(enum) if isinstance(enum, list) else set()
        mirrors = [
            (POLICY, "profile_rank", set((policy or {}).get("profile_rank") or {})),
            (CONTRACT_SCHEMA, "verification.profile enum", schema_names),
            ("scripts/loopctl.py", "PROFILE_RANK_FALLBACK", set(PROFILE_RANK_FALLBACK)),
        ]
        for where, label, names in mirrors:
            if names != canonical:
                errors.append(
                    f"verifier profile 집합 불일치: {where}의 {label} {sorted(names)} != "
                    f"정본 {REGISTRY}의 profiles {sorted(canonical)}"
                )

        # rank 값도 본다. 이름이 같아도 순서가 갈라지면 policy가 지워졌을 때
        # fallback이 같은 입력에 다른 profile을 고른다.
        policy_rank = (policy or {}).get("profile_rank") or {}
        if set(policy_rank) == set(PROFILE_RANK_FALLBACK):
            order = sorted(policy_rank, key=lambda name: policy_rank[name])
            fallback_order = sorted(PROFILE_RANK_FALLBACK, key=lambda n: PROFILE_RANK_FALLBACK[n])
            if order != fallback_order:
                errors.append(
                    f"verifier profile 순서 불일치: {POLICY} {order} != "
                    f"scripts/loopctl.py PROFILE_RANK_FALLBACK {fallback_order}"
                )

        # path_rules 밖에도 profile 이름을 쓰는 자리가 둘 더 있다. 여기가 rank에 없는
        # 이름을 가리키면 classify의 floor 비교가 -1이 되어 조용히 죽는다.
        named = [("defaults.profile", ((policy or {}).get("defaults") or {}).get("profile"))]
        for risk, name in sorted(((policy or {}).get("risk_profiles") or {}).items()):
            named.append((f"risk_profiles.{risk}", name))
        for label, name in named:
            if name and name not in policy_rank:
                errors.append(f"{POLICY}의 {label}이 profile_rank에 없는 {name}를 가리킴")

    # doctor 가 "Knowledge/Context/Evidence plane: valid" 를 찍기 전에 실제로 읽는다.
    # 깨진 claims 는 context build 를 죽이고, 없는 source 는 pack 에서 조용히 빠진다.
    claims_path = os.path.join(root, ".ai-team", "knowledge", "claims.jsonl")
    if os.path.isfile(claims_path):
        with open(claims_path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except ValueError as exc:
                    errors.append(f"claims.jsonl:{number} 파싱 실패: {exc}")

    map_path = os.path.join(root, ".ai-team", "knowledge", "map.json")
    if os.path.isfile(map_path):
        try:
            knowledge_map = load_json(map_path)
        except Exception as exc:
            errors.append(f"invalid JSON {map_path}: {exc}")
        else:
            if registry is not None:
                project = knowledge_map.get("project")
                if project and project != registry.get("project"):
                    errors.append(
                        "knowledge map project가 registry와 다름: "
                        f"{project} != {registry.get('project')}"
                    )
            for item in knowledge_map.get("sources") or []:
                rel = item.get("path")
                if rel and not os.path.exists(os.path.join(root, rel)):
                    errors.append(f"knowledge map source 없음: {item.get('id')} -> {rel}")

    template = os.path.join(root, ".ai-team", "contracts", "work-contract.template.json")
    if policy is not None and registry is not None and os.path.isfile(template):
        for error in validate_contract(template, policy, registry):
            errors.append(f"contract template: {error}")

    for rel in ("AGENTS.md", "CLAUDE.md", "CODEX.md"):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        text = open(path, encoding="utf-8").read()
        # `/grill-me`·`/feynman` 은 amplai-foundry 가 유지하는 loop 밖 보조 skill 이라
        # legacy 가 아니다 (D-046). `.ai-team/skills/` 만 legacy 로 본다.
        for old in (
            ".ai-team/gates/",
            ".ai-team/hooks/",
            ".ai-team/project/",
            ".ai-team/skills/",
        ):
            if old in text:
                errors.append(f"{rel}에 legacy reference가 남음: {old}")

    try:
        quadrant = loopv2.quadrant_audit(root)
        for item in quadrant.get("quadrants") or []:
            for missing in item.get("missing") or []:
                errors.append(
                    "Harness quadrant asset 없음: {} -> {}".format(item.get("quadrant"), missing)
                )
    except Exception as exc:
        errors.append(f"quadrant audit 실패: {exc}")

    # semantic runtime(ontology/SHACL/CQ/MCP)은 이식하지 않았다 (D-046).
    # amplai-foundry 는 Knowledge Vault lint 와 Proposal 모델이 그 자리를 대신하고,
    # 그것은 verifier registry 의 `vault-lint`/`schema` check 가 검사한다.

    if errors:
        print("LOOP DOCTOR: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1
    print("LOOP DOCTOR: PASS")
    print("- public entry points: Claude /work,/design | Codex $work,$design")
    print("- internal capabilities: %d" % (len(shared_names) - 2))
    print("- legacy runtime directories: absent")
    print("- policy/contract/verifier JSON: valid")
    print(
        "- verifier profile 집합: %s (registry 정본, policy/schema/fallback 일치)"
        % ", ".join(sorted((registry or {}).get("profiles") or {}))
    )
    print("- Knowledge/Context/Evidence plane: valid")
    print("- Semantic Runtime: 이식 제외 (D-046). Vault lint 가 대신한다")
    print("- Harness quadrant coverage: complete")
    print("- contract template: valid")
    print("- skill SSOT: .agents/skills; Claude mirror: exact symlink set")
    print("- Codex internal skill policy: implicit invocation disabled")
    if os.path.isfile(os.path.join(root, ".codex", "hooks.json")):
        print("- Codex hooks: installed and consistent with Kit state")
    return 0


def resolve_feature(root, raw=None):
    if raw:
        path = raw
    else:
        feature_file = os.path.join(root, ".specify", "feature.json")
        if not os.path.isfile(feature_file):
            return None
        try:
            path = load_json(feature_file).get("feature_directory")
        except Exception:
            return None
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(root, path)
    return os.path.normpath(path)


def feature_status(root, raw=None):
    feature = resolve_feature(root, raw)
    if not feature or not os.path.isdir(feature):
        print(
            "feature를 찾지 못했다. 경로를 주거나 .specify/feature.json을 설정한다.",
            file=sys.stderr,
        )
        return 2
    print(f"feature : {os.path.relpath(feature, root)}")
    contract_path = os.path.join(feature, "work-contract.json")
    if os.path.isfile(contract_path):
        try:
            contract = load_json(contract_path)
            print(
                "contract: {} / risk={} / profile={}".format(
                    contract.get("status"),
                    contract.get("risk"),
                    (contract.get("verification") or {}).get("profile"),
                )
            )
        except Exception as exc:
            print(f"contract: INVALID ({exc})")
    else:
        print("contract: none")

    for label, filename in (
        ("knowledge", "knowledge-readiness.json"),
        ("context", "context-pack.json"),
        ("environment", "environment.json"),
        ("handoff", "handoff.json"),
    ):
        path = os.path.join(feature, filename)
        if not os.path.isfile(path):
            print("%-8s: none" % label)
            continue
        try:
            value = load_json(path)
            if label == "knowledge":
                detail = value.get("verdict")
            elif label == "context":
                detail = "{} / {}".format(
                    value.get("knowledge_verdict"), (value.get("content_hash") or "")[:12]
                )
            elif label == "environment":
                detail = (value.get("content_hash") or "")[:12]
            else:
                detail = value.get("status")
            print("%-8s: %s" % (label, detail))
        except Exception as exc:
            print("%-8s: INVALID (%s)" % (label, exc))

    tasks_path = os.path.join(feature, "tasks.md")
    if not os.path.isfile(tasks_path):
        print("tasks   : none")
        return 0
    text = open(tasks_path, encoding="utf-8").read().splitlines()
    current = None
    slices = []
    for line in text:
        match = re.match(r"^## Slice\s+(\S+)(?:\s+—\s+(.*))?$", line)
        if match:
            current = {"id": match.group(1), "title": match.group(2) or "", "open": 0, "done": 0}
            slices.append(current)
            continue
        if current and re.match(r"^- \[[ xX]\] ", line):
            if line.startswith("- [x]") or line.startswith("- [X]"):
                current["done"] += 1
            else:
                current["open"] += 1
    print("slices  : %d" % len(slices))
    for item in slices:
        state = "PASS" if item["open"] == 0 and item["done"] > 0 else "OPEN"
        print(
            "  %-8s %-4s done=%d open=%d %s"
            % (item["id"], state, item["done"], item["open"], item["title"])
        )
    return 0


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def parse_data_arg(args):
    if getattr(args, "data_file", None):
        return load_json(args.data_file)
    if getattr(args, "data_json", None):
        return json.loads(args.data_json)
    return {}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor")

    classify = sub.add_parser("classify")
    mode = classify.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--working", action="store_true")
    classify.add_argument("--json", action="store_true")
    classify.add_argument("paths", nargs="*")

    contract = sub.add_parser("contract")
    contract_sub = contract.add_subparsers(dest="contract_command")
    validate = contract_sub.add_parser("validate")
    validate.add_argument("path")

    readiness = sub.add_parser("readiness")
    readiness_sub = readiness.add_subparsers(dest="readiness_command")
    readiness_init = readiness_sub.add_parser("init")
    readiness_init.add_argument("feature", nargs="?")
    readiness_init.add_argument("--force", action="store_true")
    readiness_eval = readiness_sub.add_parser("evaluate")
    readiness_eval.add_argument("path")
    readiness_eval.add_argument("--write", action="store_true")
    readiness_validate = readiness_sub.add_parser("validate")
    readiness_validate.add_argument("path")

    discovery = sub.add_parser("discovery")
    discovery_sub = discovery.add_subparsers(dest="discovery_command")
    discovery_scan = discovery_sub.add_parser("scan")
    discovery_scan.add_argument("feature", nargs="?")
    discovery_scan.add_argument("--dry-run", action="store_true")

    context = sub.add_parser("context")
    context_sub = context.add_subparsers(dest="context_command")
    context_build = context_sub.add_parser("build")
    context_build.add_argument("feature", nargs="?")
    context_build.add_argument("--force", action="store_true")
    context_validate = context_sub.add_parser("validate")
    context_validate.add_argument("path")

    environment = sub.add_parser("environment")
    environment_sub = environment.add_subparsers(dest="environment_command")
    environment_capture = environment_sub.add_parser("capture")
    environment_capture.add_argument("feature", nargs="?")

    permission = sub.add_parser("permission")
    permission_sub = permission.add_subparsers(dest="permission_command")
    permission_check = permission_sub.add_parser("check")
    permission_check.add_argument("action")

    trace = sub.add_parser("trace")
    trace_sub = trace.add_subparsers(dest="trace_command")
    trace_append = trace_sub.add_parser("append")
    trace_append.add_argument("feature")
    trace_append.add_argument("event")
    data_group = trace_append.add_mutually_exclusive_group()
    data_group.add_argument("--data-json")
    data_group.add_argument("--data-file")
    trace_verify = trace_sub.add_parser("verify")
    trace_verify.add_argument("path")

    handoff = sub.add_parser("handoff")
    handoff_sub = handoff.add_subparsers(dest="handoff_command")
    handoff_write = handoff_sub.add_parser("write")
    handoff_write.add_argument("feature", nargs="?")
    handoff_write.add_argument(
        "--status", choices=("ready", "executing", "blocked", "review", "done")
    )
    handoff_write.add_argument("--next-action")
    handoff_write.add_argument("--blocker", action="append", default=[])

    quadrant = sub.add_parser("quadrant")
    quadrant_sub = quadrant.add_subparsers(dest="quadrant_command")
    quadrant_sub.add_parser("audit")

    docs = sub.add_parser("docs")
    docs_sub = docs.add_subparsers(dest="docs_command")
    docs_impact = docs_sub.add_parser("impact")
    docs_impact.add_argument("feature", nargs="?")
    docs_impact.add_argument("--dry-run", action="store_true")
    docs_impact.add_argument("--acknowledge", action="append", default=[])
    docs_validate = docs_sub.add_parser("validate")
    docs_validate.add_argument("feature", nargs="?")
    docs_validate.add_argument("--repo", action="store_true")

    garden = sub.add_parser("garden")
    garden_sub = garden.add_subparsers(dest="garden_command")
    garden_sub.add_parser("scan")
    garden_incremental = garden_sub.add_parser("incremental")
    garden_incremental.add_argument("feature", nargs="?")
    garden_incremental.add_argument("--dry-run", action="store_true")
    garden_full = garden_sub.add_parser("full")
    garden_full.add_argument("--report-only", action="store_true", default=True)
    garden_full.add_argument("--apply-safe", action="store_true")
    garden_full.add_argument("--output")
    garden_apply = garden_sub.add_parser("apply")
    garden_apply.add_argument("feature", nargs="?")

    mine = sub.add_parser("mine")
    mine.add_argument("mode", choices=("bootstrap", "incremental"))
    mine.add_argument("--output", required=True)
    mine.add_argument("--scope", action="append", default=[])
    mine.add_argument("--staged", action="store_true")
    mine.add_argument("--max-files", type=int, default=4000)
    mine.add_argument("--dry-run", action="store_true")

    status = sub.add_parser("status")
    status.add_argument("feature", nargs="?")
    return parser


def main(argv=None):
    root = root_dir()
    os.chdir(root)
    args = build_parser().parse_args(argv or sys.argv[1:])

    try:
        if args.command == "doctor":
            return doctor(root)

        if args.command == "classify":
            policy = load_json(os.path.join(root, POLICY))
            paths = args.paths
            if args.staged:
                paths = working_paths(staged=True)
            elif args.working or not paths:
                paths = working_paths(staged=False)
            result = classify_paths(policy, paths)
            print_classification(result, args.json)
            return 0

        if args.command == "contract" and args.contract_command == "validate":
            policy = load_json(os.path.join(root, POLICY))
            registry = load_json(os.path.join(root, REGISTRY))
            errors = validate_contract(args.path, policy, registry)
            if errors:
                print("CONTRACT: FAIL")
                for error in errors:
                    print(f"- {error}")
                return 1
            print("CONTRACT: PASS")
            return 0

        if args.command == "readiness" and args.readiness_command == "init":
            print_json(loopv2.readiness_init(root, args.feature, args.force))
            return 0
        if args.command == "readiness" and args.readiness_command == "evaluate":
            result = loopv2.readiness_evaluate(root, args.path, args.write)
            print_json(result)
            return (
                0 if result["verdict"] == "READY" else (3 if result["verdict"] == "DISCOVER" else 4)
            )
        if args.command == "readiness" and args.readiness_command == "validate":
            result = loopv2.readiness_validate(root, args.path)
            print_json(result)
            return 0 if result["valid"] else 1

        if args.command == "discovery" and args.discovery_command == "scan":
            print_json(loopv2.discovery_scan(root, args.feature, write=not args.dry_run))
            return 0

        if args.command == "context" and args.context_command == "build":
            print_json(loopv2.context_build(root, args.feature, args.force))
            return 0
        if args.command == "context" and args.context_command == "validate":
            result = loopv2.context_validate(root, args.path)
            print_json(result)
            return 0 if result["valid"] else 1

        if args.command == "environment" and args.environment_command == "capture":
            print_json(loopv2.environment_capture(root, args.feature))
            return 0

        if args.command == "permission" and args.permission_command == "check":
            result = loopv2.permission_check(root, args.action)
            print_json(result)
            return {"allowed": 0, "gated": 3, "prohibited": 4}.get(result.get("level"), 4)

        if args.command == "trace" and args.trace_command == "append":
            print_json(loopv2.trace_append(root, args.feature, args.event, parse_data_arg(args)))
            return 0
        if args.command == "trace" and args.trace_command == "verify":
            result = loopv2.trace_verify(root, args.path)
            print_json(result)
            return 0 if result["valid"] else 1

        if args.command == "handoff" and args.handoff_command == "write":
            print_json(
                loopv2.handoff_write(
                    root, args.feature, args.status, args.next_action, args.blocker
                )
            )
            return 0

        if args.command == "quadrant" and args.quadrant_command == "audit":
            result = loopv2.quadrant_audit(root)
            print_json(result)
            return 0 if result["pass"] else 1

        if args.command == "docs" and args.docs_command == "impact":
            result = loopv2.docs_impact(
                root, args.feature, write=not args.dry_run, acknowledged=args.acknowledge
            )
            print_json(result)
            return 0 if result["status"] != "STALE" else 3
        if args.command == "docs" and args.docs_command == "validate":
            result = loopv2.docs_validate(root, args.feature, repo=args.repo)
            print_json(result)
            return 0 if result["valid"] else 1

        if args.command == "garden" and args.garden_command == "scan":
            result = loopv2.garden_scan(root)
            print_json(result)
            return 0 if result["pass"] else 1
        if args.command == "garden" and args.garden_command == "incremental":
            result = loopv2.garden_incremental(root, args.feature, write=not args.dry_run)
            print_json(result)
            return 0 if result["pass"] else 1
        if args.command == "garden" and args.garden_command == "full":
            # 기본은 report-only 다. 삭제는 --apply-safe 를 명시할 때만 한다.
            result = loopv2.garden_full(root, report_only=not args.apply_safe, output=args.output)
            print_json(result)
            return 0 if result["pass"] else 1
        if args.command == "garden" and args.garden_command == "apply":
            result = loopv2.garden_apply(root, args.feature)
            print_json(result)
            return 0 if result["pass"] else 1

        if args.command == "mine":
            command = [
                sys.executable,
                "tools/knowledge/project_miner.py",
                args.mode,
                "--output",
                args.output,
                "--max-files",
                str(args.max_files),
            ]
            for scope in args.scope:
                command.extend(["--scope", scope])
            if args.staged:
                command.append("--staged")
            if args.dry_run:
                command.append("--dry-run")
            return subprocess.call(command, cwd=root)

        if args.command == "status":
            return feature_status(root, args.feature)
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print("LOOPCTL: ERROR")
        print(f"- {exc}")
        return 2

    build_parser().print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Knowledge, evidence, and governance utilities for AMPLAI Loop Runtime V2.

This module is called by ``scripts/loopctl.py``.  It does not orchestrate model
turns; it makes the V2 gates and artifacts deterministic and inspectable.
"""

import copy
import datetime
import fnmatch
import hashlib
import json
import os
import platform
import re
import subprocess
import sys

import amplai_docs

READINESS_POLICY = os.path.join(".ai-team", "policy", "knowledge-readiness.json")
PERMISSION_POLICY = os.path.join(".ai-team", "policy", "permissions.json")
QUADRANT_POLICY = os.path.join(".ai-team", "policy", "quadrant.json")
DOCUMENTATION_POLICY = os.path.join(".ai-team", "policy", "documentation.json")
GARDENING_POLICY = os.path.join(".ai-team", "policy", "gardening.json")
KNOWLEDGE_MAP = os.path.join(".ai-team", "knowledge", "map.json")
CLAIMS = os.path.join(".ai-team", "knowledge", "claims.jsonl")
DECISIONS = os.path.join(".ai-team", "knowledge", "decisions.index.json")

REGISTRY = os.path.join(".ai-team", "verifiers", "registry.json")
UTC = getattr(datetime, "UTC", None) or datetime.timezone.utc  # noqa: UP017 -- Python 3.10 support

# 파일 경로가 아닌 evidence locator. superseded claim 의 근거는 이미 삭제된
# 파일인 게 정상이므로 이런 marker 를 stale 로 세지 않는다.
EVIDENCE_MARKERS = ("git-history",)

REPOSITORY_PROFILE = os.path.join(".ai-team", "runtime", "repository-profile.json")
GENERIC_SKILLS = frozenset(
    (
        "work",
        "design",
        "dev-loop",
        "speckit-specify",
        "speckit-clarify",
        "speckit-plan",
        "taskify",
        "speckit-analyze",
        "speckit-implement",
        "speckit-converge",
        "code-review",
        "systematic-debugging",
    )
)


def repository_profile(root):
    """Read the repository adapter; mandatory generic capabilities cannot be removed."""
    value = load_json(os.path.join(root, REPOSITORY_PROFILE))
    if not isinstance(value, dict) or value.get("schema_version") != "1.0":
        raise ValueError("invalid repository profile schema")
    if value.get("id") not in ("generic", "foundry"):
        raise ValueError("unknown repository profile")
    for key in ("additional_skills", "public_helpers", "forbidden_paths", "fixture_refs"):
        items = value.get(key)
        if not isinstance(items, list) or any(not isinstance(x, str) or not x for x in items):
            raise ValueError("invalid repository profile field: " + key)
        if len(items) != len(set(items)):
            raise ValueError("duplicate repository profile field: " + key)
    extras = set(value["additional_skills"])
    if extras & GENERIC_SKILLS or any(not re.match(r"^[a-z0-9][a-z0-9-]*$", x) for x in extras):
        raise ValueError("invalid additional skill")
    if not set(value["public_helpers"]) <= extras:
        raise ValueError("public helpers must be declared additional skills")
    return value


def verifier_interpreters(root):
    """registry 의 check 가 실제로 부르는 python interpreter 를 뽑는다.

    상수로 적어 두면 registry 가 다른 interpreter 를 쓰기 시작해도 fingerprint 는
    옛 주장을 계속 한다. 관찰값만 기록한다.

    command 를 해석하지 못하면 빈 목록이 아니라 그 사실을 돌려준다 — 관찰 실패를
    관찰 결과로 착각하면 이 함수의 존재 이유가 없어진다.
    """
    try:
        registry = load_json(os.path.join(root, REGISTRY))
    except Exception as exc:
        return [f"unobserved: registry를 읽지 못함 ({exc})"]
    found = []
    unparsed = []
    for item in registry.get("checks") or []:
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        head = command.split() if isinstance(command, str) else []
        if not head:
            continue
        if os.path.basename(head[0]) in ("python", "python3") or head[0].endswith("/python"):
            if head[0] not in found:
                found.append(head[0])
        elif "python" in command:
            # env prefix, bash -c wrapper 처럼 첫 토큰이 interpreter 가 아닌 형태.
            unparsed.append(item.get("id") or command)
    values = sorted(found)
    if unparsed:
        values.append(
            "unobserved: command에서 interpreter를 못 읽음 ({})".format(", ".join(sorted(unparsed)))
        )
    return values


def python_requirement(root):
    """pyproject.toml 의 requires-python 을 읽는다. 못 읽으면 빈 문자열."""
    path = os.path.join(root, "pyproject.toml")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("requires-python") and "=" in stripped:
                    return stripped.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        return ""
    return ""


def utc_now():
    return datetime.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, value):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def read_jsonl(path):
    values = []
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            value = json.loads(text)
            value.setdefault("_line", line_no)
            values.append(value)
    return values


def append_jsonl(path, value):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def file_sha(path):
    if not path or not os.path.isfile(path):
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def object_sha(value):
    clean = copy.deepcopy(value)
    if isinstance(clean, dict):
        clean.pop("content_hash", None)
        clean.pop("event_hash", None)
        # read_jsonl adds parser metadata that is not part of the persisted event.
        # Hash verification must use exactly the fields written to disk.
        clean.pop("_line", None)
    text = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git(root, args):
    try:
        out = subprocess.check_output(
            [*amplai_docs.GIT_READ_PREFIX, *list(args)], cwd=root, stderr=subprocess.STDOUT
        )
        return out.decode("utf-8", "replace").strip()
    except Exception:
        return ""


# Provenance markers. WORKTREE는 아직 commit되지 않은 상태를 정직하게 표시한다.
# UNVERIFIABLE은 이 repository에서 재검증할 수 없는 외부 출처를 표시한다.
PROVENANCE_WORKTREE = "WORKTREE"
PROVENANCE_UNVERIFIABLE = "UNVERIFIABLE"
PROVENANCE_MARKERS = (PROVENANCE_WORKTREE, PROVENANCE_UNVERIFIABLE)


def git_ok(root, args):
    try:
        subprocess.check_output(
            [*amplai_docs.GIT_READ_PREFIX, *list(args)], cwd=root, stderr=subprocess.STDOUT
        )
        return True
    except Exception:
        return False


def git_commit_exists(root, commit):
    """실제 Git object가 있는지 확인한다. marker는 조회 대상이 아니다."""
    if not commit or commit in PROVENANCE_MARKERS:
        return True
    return git_ok(root, ["cat-file", "-e", f"{commit}^{{commit}}"])


def provenance_commit_state(root, commit):
    """commit 참조의 상태를 사람이 읽을 수 있는 값으로 돌려준다.

    marker를 'exists=True'로 뭉뚱그리면 검증한 것처럼 읽힌다.
    """
    if not commit:
        return "ABSENT"
    if commit == PROVENANCE_WORKTREE:
        return "WORKTREE"
    if commit == PROVENANCE_UNVERIFIABLE:
        return "UNVERIFIABLE"
    return "PRESENT" if git_commit_exists(root, commit) else "NOT_FOUND"


def worktree_state(root):
    """현재 commit과 dirty 여부를 함께 남긴다.

    commit 전 working tree를 쓰는 Work를 거짓 commit으로 바꾸지 않으려면,
    HEAD만으로는 부족하다 — HEAD가 있어도 tree가 변경돼 있으면 그 HEAD는
    실제로 검증에 쓰인 상태가 아니다.
    """
    head = git(root, ["rev-parse", "HEAD"])
    dirty = git(root, ["status", "--porcelain"]).strip() != ""
    return {
        "commit": head or PROVENANCE_WORKTREE,
        "dirty": dirty,
        "state": PROVENANCE_WORKTREE if (dirty or not head) else "COMMIT",
    }


def source_state(root, path, recorded_sha=""):
    """source 하나의 현재 상태를 MATCH/STALE/MISSING으로 판정한다."""
    if not path:
        return {"path": path, "state": "MISSING", "reason": "path 없음"}
    full = os.path.join(root, path)
    if not os.path.exists(full):
        return {"path": path, "state": "MISSING", "reason": "파일이 없음"}
    if not recorded_sha:
        return {"path": path, "state": "UNTRACKED", "reason": "기록된 hash 없음"}
    current = file_sha(full)
    if current == recorded_sha:
        return {"path": path, "state": "MATCH"}
    return {
        "path": path,
        "state": "STALE",
        "reason": "hash 불일치",
        "recorded_sha256": recorded_sha,
        "current_sha256": current,
    }


def resolve_feature(root, raw):
    path = raw
    if not path:
        marker = os.path.join(root, ".specify", "feature.json")
        if not os.path.isfile(marker):
            raise RuntimeError("feature 경로가 없고 .specify/feature.json도 없다")
        path = load_json(marker).get("feature_directory")
    if not path:
        raise RuntimeError("feature directory를 결정하지 못함")
    if not os.path.isabs(path):
        path = os.path.join(root, path)
    return os.path.normpath(path)


def rel(root, path):
    return os.path.relpath(path, root).replace("\\", "/")


def safe_path(root, path):
    candidate = path if os.path.isabs(path) else os.path.join(root, path)
    candidate = os.path.realpath(candidate)
    root_real = os.path.realpath(root)
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        raise RuntimeError(f"repository 밖 경로는 허용되지 않음: {path}")
    return candidate


def contract_path(feature):
    return os.path.join(feature, "work-contract.json")


def readiness_path(feature):
    return os.path.join(feature, "knowledge-readiness.json")


def discovery_path(feature):
    return os.path.join(feature, "domain-discovery.json")


def context_path(feature):
    return os.path.join(feature, "context-pack.json")


def handoff_path(feature):
    return os.path.join(feature, "handoff.json")


def environment_path(feature):
    return os.path.join(feature, "environment.json")


def trace_path(feature):
    return os.path.join(feature, "evidence-trace.jsonl")


def default_check(status="unknown", evidence=None, notes=""):
    return {"status": status, "evidence": list(evidence or []), "notes": notes}


def evaluate_readiness_value(value, policy):
    required = policy.get("required_checks") or []
    errors = []
    checks = value.get("checks")
    if not isinstance(checks, dict):
        return "BLOCKED", ["checks가 object가 아님"]
    statuses = []
    for key in required:
        item = checks.get(key)
        if not isinstance(item, dict):
            errors.append(f"check 없음: {key}")
            statuses.append("unknown")
            continue
        status = item.get("status")
        statuses.append(status)
        if status not in ("ready", "unknown", "conflict", "not_applicable"):
            errors.append(f"check status 오류: {key}={status}")
        evidence = item.get("evidence")
        if status == "ready" and not evidence:
            errors.append(f"ready check는 evidence가 필요함: {key}")
    contradictions = value.get("contradictions") or []
    blocking = value.get("blocking_unknowns") or []
    if errors or contradictions or "conflict" in statuses:
        return "BLOCKED", errors
    if blocking or "unknown" in statuses:
        return "DISCOVER", errors
    return "READY", errors


def readiness_init(root, feature_raw, force=False):
    feature = resolve_feature(root, feature_raw)
    contract = load_json(contract_path(feature))
    policy = load_json(os.path.join(root, READINESS_POLICY))
    path = readiness_path(feature)
    if os.path.exists(path) and not force:
        raise RuntimeError("knowledge-readiness.json이 이미 있음; --force가 필요함")
    work_type = contract.get("work_type") or "new_feature"
    risk = contract.get("risk") or "normal"
    required = work_type in (policy.get("mandatory_for_work_types") or []) or risk in (
        policy.get("mandatory_for_risks") or []
    )
    bypass = work_type in (policy.get("fast_path_work_types") or []) and risk == "low"
    checks = {}
    for key in policy.get("required_checks") or []:
        if bypass:
            checks[key] = default_check(
                "not_applicable", [f"fast-path:{work_type}"], "LOW tiny change"
            )
        else:
            checks[key] = default_check("unknown" if required else "not_applicable")
    value = {
        "schema_version": "1.0",
        "work_id": contract.get("id"),
        "work_type": work_type,
        "risk": risk,
        "checks": checks,
        "blocking_unknowns": []
        if bypass
        else (["repository/domain evidence 조사 필요"] if required else []),
        "contradictions": [],
        "verdict": "READY" if bypass or not required else "DISCOVER",
        "generated_from": {
            "contract": rel(root, contract_path(feature)),
            "commit": git(root, ["rev-parse", "HEAD"]) or "WORKTREE",
        },
    }
    if bypass:
        value["bypass_reason"] = "LOW tiny_change fast path"
    write_json(path, value)
    return value


def readiness_validate(root, raw):
    path = safe_path(root, raw)
    value = load_json(path)
    policy = load_json(os.path.join(root, READINESS_POLICY))
    required_keys = [
        "schema_version",
        "work_id",
        "work_type",
        "risk",
        "checks",
        "blocking_unknowns",
        "contradictions",
        "verdict",
        "generated_from",
    ]
    errors = [f"필수 필드 없음: {key}" for key in required_keys if key not in value]
    computed, extra = evaluate_readiness_value(value, policy)
    errors.extend(extra)
    if value.get("verdict") != computed:
        errors.append(
            "verdict 불일치: stored={} computed={}".format(value.get("verdict"), computed)
        )
    return {
        "valid": not errors,
        "computed_verdict": computed,
        "errors": errors,
        "path": rel(root, path),
    }


def readiness_evaluate(root, raw, write=False):
    path = safe_path(root, raw)
    value = load_json(path)
    policy = load_json(os.path.join(root, READINESS_POLICY))
    verdict, errors = evaluate_readiness_value(value, policy)
    value["verdict"] = verdict
    if write:
        write_json(path, value)
    return {"verdict": verdict, "errors": errors, "value": value, "path": rel(root, path)}


def tokenize(text):
    values = re.findall(r"[A-Za-z][A-Za-z0-9_.-]*|[가-힣]{2,}|[0-9]+", text or "")
    result = []
    for value in values:
        low = value.lower()
        if len(low) >= 2 and low not in result:
            result.append(low)
    return result


def grep_candidates(root, terms, max_results=80):
    candidates = {}
    for term in terms[:30]:
        try:
            out = subprocess.check_output(
                [
                    "git",
                    "grep",
                    "-n",
                    "-I",
                    "-F",
                    term,
                    "--",
                    "src",
                    "include",
                    "plugins",
                    "tools",
                    "tests",
                    "conf",
                    "docs",
                    "specs",
                ],
                cwd=root,
                stderr=subprocess.STDOUT,
            ).decode("utf-8", "replace")
        except subprocess.CalledProcessError:
            out = ""
        for line in out.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            path, line_no, snippet = parts
            item = candidates.setdefault(
                path, {"path": path, "score": 0.0, "matches": [], "locators": []}
            )
            if term not in item["matches"]:
                item["matches"].append(term)
                item["score"] += 1.0
            locator = f"line:{line_no}:{snippet.strip()[:160]}"
            if locator not in item["locators"] and len(item["locators"]) < 6:
                item["locators"].append(locator)
        if len(candidates) >= max_results:
            break
    values = list(candidates.values())
    values.sort(key=lambda item: (-item["score"], item["path"]))
    return values[:max_results]


def discovery_scan(root, feature_raw, write=True):
    feature = resolve_feature(root, feature_raw)
    contract = load_json(contract_path(feature))
    terms = tokenize(
        " ".join(
            [
                contract.get("goal") or "",
                contract.get("title") or "",
                " ".join((contract.get("scope") or {}).get("include") or []),
                " ".join(item.get("statement") or "" for item in contract.get("acceptance") or []),
            ]
        )
    )
    candidates = grep_candidates(root, terms)
    known = []
    invariants = []
    for item in candidates[:30]:
        fact = {
            "statement": "repository evidence candidate: {}".format(item["path"]),
            "status": "candidate",
            "confidence": "high" if item["score"] >= 3 else "medium",
            "evidence": [
                {"type": "repository", "source": item["path"], "locator": locator}
                for locator in item.get("locators") or []
            ],
        }
        known.append(fact)
        if (
            item["path"].startswith("tests/")
            or item["path"].startswith("tools/ci/")
            or "/rules/" in item["path"]
        ):
            invariants.append(fact)
    value = {
        "schema_version": "1.0",
        "work_id": contract.get("id"),
        "terms": terms,
        "evidence_candidates": candidates,
        "known_facts": known,
        "invariants": invariants,
        "contradictions": [],
        "unknowns": [
            "candidate evidence를 검토해 ownership/source-of-truth를 확정해야 함",
            "acceptance와 verifier가 실제 current behavior를 판정하는지 확인해야 함",
        ],
        "blocking_questions": [],
        "generated_from": {
            "contract": rel(root, contract_path(feature)),
            "commit": git(root, ["rev-parse", "HEAD"]) or "WORKTREE",
            "working_diff": git(root, ["diff", "--name-only"]),
        },
    }
    if write:
        write_json(discovery_path(feature), value)
    return value


def selected_sources(root, contract, max_sources=None):
    knowledge = load_json(os.path.join(root, KNOWLEDGE_MAP))
    accepted, selection = amplai_docs.knowledge_filter(root, contract)
    mandatory = {
        item["path"] for item in selection["governing_inputs"] if item["role"] == "instruction"
    }
    query = " ".join(
        [
            contract.get("goal") or "",
            contract.get("title") or "",
            " ".join((contract.get("scope") or {}).get("include") or []),
        ]
    ).lower()
    tokens = tokenize(query)
    exclude = set((knowledge.get("selection") or {}).get("exclude_status") or [])
    limit = max_sources or (knowledge.get("selection") or {}).get("max_sources") or 24
    values = []
    for item in knowledge.get("sources") or []:
        if item.get("status") in exclude or not accepted(item):
            continue
        score = float(item.get("priority") or 0) / 1000.0
        hits = []
        text = " ".join(
            [item.get("id") or "", item.get("path") or "", " ".join(item.get("keywords") or [])]
        ).lower()
        for token in tokens:
            if token in text:
                hits.append(token)
                score += 1.0
        if item.get("path") in mandatory or (item.get("priority") or 0) >= 90 or hits:
            selected = copy.deepcopy(item)
            selected["score"] = round(score, 4)
            selected["reason"] = (
                "keyword:{}".format(",".join(hits)) if hits else "high-priority-core"
            )
            values.append(selected)
    values.sort(
        key=lambda item: (
            item.get("path") not in mandatory,
            -item["score"],
            -(item.get("priority") or 0),
            item.get("id") or "",
        )
    )
    selected = values[:limit]
    if not mandatory <= {item["path"] for item in selected}:
        raise amplai_docs.DocumentError("GOVERNING_INPUT_UNAVAILABLE")
    return selected


def active_claims(root, contract):
    scope = (contract.get("scope") or {}).get("include") or []
    accepted, _ = amplai_docs.knowledge_filter(root, contract)
    values = []
    for item in read_jsonl(os.path.join(root, CLAIMS)):
        if not accepted(
            item, [x for x in item.get("evidence") or [] if isinstance(x, dict)], kind="claim"
        ):
            continue
        claim_scope = item.get("scope") or []
        if (
            not scope
            or not claim_scope
            or any(a.startswith(b) or b.startswith(a) for a in scope for b in claim_scope)
        ):
            values.append(item)
    values.sort(key=lambda item: item.get("id") or "")
    return values


def active_decisions(root, contract):
    value = load_json(os.path.join(root, DECISIONS))
    accepted, _ = amplai_docs.knowledge_filter(root, contract)
    scope = (contract.get("scope") or {}).get("include") or []
    result = []
    for item in value.get("entries") or []:
        if not amplai_docs.scope_applies(contract, item) or not accepted(item, kind="decision"):
            continue
        item_scope = item.get("scope") or []
        if (
            not scope
            or not item_scope
            or any(a.startswith(b) or b.startswith(a) for a in scope for b in item_scope)
        ):
            result.append(copy.deepcopy(item))
    result.sort(key=lambda item: item.get("id") or "")
    return result


def derive_test_scope(root, code_scope):
    result = []
    tracked = git(root, ["ls-files", "tests", "tools/ci"]).splitlines()
    stems = set()
    for path in code_scope:
        stem = os.path.splitext(os.path.basename(path.rstrip("/")))[0]
        if len(stem) >= 4:
            stems.add(stem.lower())
    for path in tracked:
        low = path.lower()
        if not stems or any(stem in low for stem in stems):
            result.append(path)
        if len(result) >= 40:
            break
    return result


def context_records(root, contract):
    """Canonical complete records, shared by generation and validation."""
    sources = selected_sources(root, contract)
    claims = active_claims(root, contract)
    decisions = active_decisions(root, contract)
    required = []
    for item in sources:
        path = item.get("path")
        required.append(
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "path": path,
                "reason": item.get("reason"),
                "source_sha256": file_sha(safe_path(root, path)),
                "evidence": [],
            }
        )
    for item in claims:
        path = item.get("evidence", [{}])[0].get("path", CLAIMS)
        required.append(
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "path": path,
                "reason": "active claim",
                "source_sha256": file_sha(safe_path(root, path)),
                "evidence": [
                    {**entry, "source_sha256": file_sha(safe_path(root, entry["path"]))}
                    for entry in item.get("evidence") or []
                ],
            }
        )
    active = [
        {
            "id": item.get("id"),
            "status": item.get("status"),
            "path": item.get("path"),
            "reason": item.get("title"),
            "source_sha256": file_sha(safe_path(root, item["path"])),
            "evidence": [],
        }
        for item in decisions
    ]
    return required, active


def context_build(root, feature_raw, force=False):
    feature = resolve_feature(root, feature_raw)
    target = context_path(feature)
    if os.path.exists(target) and not force:
        raise RuntimeError("context-pack.json이 이미 있음; --force가 필요함")
    contract_file = contract_path(feature)
    contract = load_json(contract_file)
    readiness_file = readiness_path(feature)
    readiness = load_json(readiness_file) if os.path.isfile(readiness_file) else None
    if readiness:
        report = readiness_evaluate(root, readiness_file, write=False)
        if report["verdict"] != "READY":
            raise RuntimeError("Knowledge Readiness가 READY가 아님: {}".format(report["verdict"]))
        verdict = "READY"
    else:
        work_type = contract.get("work_type") or "new_feature"
        if work_type not in ("tiny_change",):
            raise RuntimeError("knowledge-readiness.json이 없음")
        verdict = "BYPASS"
    selection = amplai_docs.context_selection(root, contract)
    required_knowledge, decisions = context_records(root, contract)
    if amplai_docs.context_selection(root, contract) != selection:
        raise amplai_docs.DocumentError("SOURCE_DRIFT")
    code_scope = list((contract.get("scope") or {}).get("include") or [])
    tests = derive_test_scope(root, code_scope)
    # semantic runtime 은 이식하지 않았다 (D-046). schema 가 요구하는 필드라 빈 채로 둔다.
    ontology_refs = []
    tree = worktree_state(root)
    generated = {
        "commit": tree["commit"],
        "worktree_state": tree["state"],
        "worktree_dirty": tree["dirty"],
        "generated_at": utc_now(),
        "contract_sha256": file_sha(contract_file),
        "knowledge_map_sha256": file_sha(os.path.join(root, KNOWLEDGE_MAP)),
        "claims_sha256": file_sha(os.path.join(root, CLAIMS)),
        "decisions_sha256": file_sha(os.path.join(root, DECISIONS)),
        "readiness_sha256": file_sha(readiness_file),
        "discovery_sha256": file_sha(discovery_path(feature)),
        "document_selection_sha256": amplai_docs.object_digest(selection),
    }
    value = {
        "schema_version": "1.0",
        "work_id": contract.get("id"),
        "goal": contract.get("goal"),
        "knowledge_verdict": verdict,
        "required_knowledge": required_knowledge,
        "active_decisions": decisions,
        "ontology_refs": ontology_refs,
        "code_scope": code_scope,
        "test_scope": tests,
        "verifier_profile": (contract.get("verification") or {}).get("profile"),
        "unknowns": list(readiness.get("blocking_unknowns") or []) if readiness else [],
        "generated_from": generated,
    }
    value["content_hash"] = object_sha(value)
    write_json(target, value)
    return value


def context_validate(root, raw):
    path = safe_path(root, raw)
    value = load_json(path)
    required = [
        "schema_version",
        "work_id",
        "goal",
        "knowledge_verdict",
        "required_knowledge",
        "active_decisions",
        "ontology_refs",
        "code_scope",
        "test_scope",
        "verifier_profile",
        "unknowns",
        "generated_from",
        "content_hash",
    ]
    errors = [f"필수 필드 없음: {key}" for key in required if key not in value]
    if value.get("knowledge_verdict") not in ("READY", "BYPASS"):
        errors.append("knowledge_verdict가 READY/BYPASS가 아님")
    if value.get("unknowns"):
        errors.append("Context Pack에 unresolved unknown이 남음")
    expected = object_sha(value)
    if value.get("content_hash") != expected:
        errors.append("content_hash 불일치")

    # provenance: 기록된 commit이 실제 Git object인지 확인한다.
    # 없는 commit을 근거로 남기면 재현이 불가능하다.
    generated = value.get("generated_from") or {}
    contract_file = os.path.join(os.path.dirname(path), "work-contract.json")
    if not os.path.isfile(contract_file):
        raise amplai_docs.DocumentError("MISSING_CONTRACT")
    current_contract = load_json(contract_file)
    for key, source in (
        ("contract_sha256", contract_file),
        ("knowledge_map_sha256", os.path.join(root, KNOWLEDGE_MAP)),
        ("claims_sha256", os.path.join(root, CLAIMS)),
        ("decisions_sha256", os.path.join(root, DECISIONS)),
        ("readiness_sha256", os.path.join(os.path.dirname(path), "knowledge-readiness.json")),
        ("discovery_sha256", os.path.join(os.path.dirname(path), "domain-discovery.json")),
    ):
        if key not in generated or generated[key] != file_sha(source):
            errors.append("context control input changed; rebuild Context Pack")
    accepted, selection = amplai_docs.knowledge_filter(root, current_contract)
    selected_digest = generated.get("document_selection_sha256")
    if selected_digest != amplai_docs.object_digest(selection):
        errors.append("document selection changed; rebuild Context Pack")
    expected_knowledge, expected_decisions = context_records(root, current_contract)
    if value.get("required_knowledge") != expected_knowledge:
        errors.append("current knowledge set incomplete or changed; rebuild Context Pack")
    if value.get("active_decisions") != expected_decisions:
        errors.append("active decision set incomplete or changed; rebuild Context Pack")
    commit = generated.get("commit")
    if commit and not git_commit_exists(root, commit):
        errors.append(f"provenance commit이 이 repository에 없음: {commit}")

    # source 상태: 기록된 hash와 현재 파일을 다시 비교해 stale을 잡는다.
    sources = []
    stale = []
    missing = []
    superseded = []
    for item in value.get("required_knowledge") or []:
        if item not in expected_knowledge:
            continue  # Never echo a forged or newly unauthorized Context reference.
        evidence_paths = [x for x in item.get("evidence") or [] if isinstance(x, dict)]
        if not accepted(item, evidence_paths or None, kind="claim" if evidence_paths else "source"):
            superseded.append("INELIGIBLE_CURRENT_KNOWLEDGE")
            errors.append("current knowledge no longer eligible; rebuild Context Pack")
            continue
        status = item.get("status")
        if status in ("deprecated", "superseded", "rejected"):
            superseded.append(item.get("id"))
            errors.append("inactive knowledge가 선택됨: {}".format(item.get("id")))
        state = source_state(root, item.get("path"), item.get("source_sha256") or "")
        state["id"] = item.get("id")
        sources.append(state)
        if state["state"] == "STALE":
            stale.append(item.get("path"))
        elif state["state"] == "MISSING":
            missing.append(item.get("path"))
        for entry in item.get("evidence") or []:
            state = source_state(root, entry.get("path"), entry.get("source_sha256") or "")
            if not entry.get("source_sha256") or state["state"] != "MATCH":
                errors.append("claim evidence changed or unbound; rebuild Context Pack")

    for item in value.get("active_decisions") or []:
        if item not in expected_decisions:
            continue
        if not accepted(item, kind="decision"):
            errors.append("current decision no longer eligible; rebuild Context Pack")
        elif (
            not item.get("source_sha256")
            or source_state(root, item.get("path"), item["source_sha256"])["state"] != "MATCH"
        ):
            errors.append("decision source changed; rebuild Context Pack")

    for path_ in missing:
        errors.append(f"source가 없음: {path_}")
    for path_ in stale:
        errors.append(f"source가 생성 이후 변경됨(STALE): {path_}")

    if stale:
        verdict = "STALE"
    elif missing:
        verdict = "MISSING"
    elif superseded:
        verdict = "SUPERSEDED"
    elif errors:
        verdict = "INVALID"
    else:
        verdict = "MATCH"

    return {
        "valid": not errors,
        "verdict": verdict,
        "errors": errors,
        "path": rel(root, path),
        "expected_hash": expected,
        "provenance": {
            "commit": commit,
            "commit_state": provenance_commit_state(root, commit),
            "worktree_state": generated.get("worktree_state"),
        },
        "sources": sources,
    }


def command_version(command, cwd):
    try:
        proc = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output, _ = proc.communicate()
        text = output.decode("utf-8", "replace").strip().splitlines()
        return {
            "command": " ".join(command),
            "exit_code": proc.returncode,
            "output": text[0] if text else "",
        }
    except Exception as exc:
        return {"command": " ".join(command), "exit_code": None, "error": str(exc)}


def environment_capture(root, feature_raw):
    feature = resolve_feature(root, feature_raw)
    contract = load_json(contract_path(feature))
    value = {
        "schema_version": "1.0",
        "work_id": contract.get("id"),
        "captured_at": utc_now(),
        "git": {
            "commit": git(root, ["rev-parse", "HEAD"]) or "WORKTREE",
            "branch": git(root, ["rev-parse", "--abbrev-ref", "HEAD"]),
            "dirty": bool(git(root, ["status", "--porcelain=v1"])),
        },
        "host": {
            "platform": platform.platform(),
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "tools": [command_version([sys.executable, "--version"], root)]
        + [command_version([name, "--version"], root) for name in verifier_interpreters(root)]
        + [command_version(["git", "--version"], root)],
        # verifier 가 실제로 증명한 경계만 쓴다. 증명하지 않은 platform 을 여기 적으면
        # fingerprint 가 존재 이유를 잃는다.
        "target_assumptions": {
            "verified_on": platform.platform(),
            "python_requirement": python_requirement(root),
            "verifier_interpreter": verifier_interpreters(root),
            "not_verified": [
                "다른 OS·architecture 에서의 동작",
                "pyproject 의 requires-python 아래 버전에서의 동작",
            ],
        },
        "fixture_refs": repository_profile(root)["fixture_refs"],
        # Local paths and environment values are not portable evidence. Interpreter
        # commands/versions above describe the actual verifier without exporting them.
        "safe_environment": {},
    }
    value["content_hash"] = object_sha(value)
    write_json(environment_path(feature), value)
    return value


def permission_check(root, action):
    policy = load_json(os.path.join(root, PERMISSION_POLICY))
    item = (policy.get("actions") or {}).get(action)
    if not item:
        return {
            "action": action,
            "level": "prohibited",
            "reason": "policy에 정의되지 않은 action은 기본 차단",
        }
    value = copy.deepcopy(item)
    value["action"] = action
    return value


def trace_append(root, feature_raw, event, data=None):
    feature = resolve_feature(root, feature_raw)
    path = trace_path(feature)
    history = read_jsonl(path)
    previous_hash = history[-1].get("event_hash") if history else "GENESIS"
    value = {
        "schema_version": "1.0",
        "sequence": len(history) + 1,
        "work_id": load_json(contract_path(feature)).get("id"),
        "timestamp": utc_now(),
        "event": event,
        "data": data or {},
        "previous_hash": previous_hash,
        "git_checkpoint": git(root, ["rev-parse", "HEAD"]) or "WORKTREE",
    }
    value["event_hash"] = object_sha(value)
    append_jsonl(path, value)
    return value


def trace_verify(root, raw):
    path = safe_path(root, raw)
    history = read_jsonl(path)
    errors = []
    previous = "GENESIS"
    for idx, item in enumerate(history, start=1):
        if item.get("sequence") != idx:
            errors.append(f"sequence 불일치: {idx}")
        if item.get("previous_hash") != previous:
            errors.append(f"previous_hash 불일치: {idx}")
        expected = object_sha(item)
        if item.get("event_hash") != expected:
            errors.append(f"event_hash 불일치: {idx}")
        previous = item.get("event_hash")
    return {"valid": not errors, "events": len(history), "errors": errors, "path": rel(root, path)}


def parse_tasks(feature):
    path = os.path.join(feature, "tasks.md")
    completed = []
    open_slices = []
    current = None
    counts = {}
    if not os.path.isfile(path):
        return completed, open_slices, None
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            match = re.match(r"^## Slice\s+(\S+)", line)
            if match:
                current = match.group(1)
                counts[current] = [0, 0]
                continue
            if current and line.startswith("- [x]"):
                counts[current][0] += 1
            elif current and line.startswith("- [ ]"):
                counts[current][1] += 1
    for slice_id, count in counts.items():
        if count[1] == 0 and count[0] > 0:
            completed.append(slice_id)
        else:
            open_slices.append(slice_id)
    current = open_slices[0] if open_slices else None
    return completed, open_slices, current


def handoff_write(root, feature_raw, status=None, next_action=None, blockers=None):
    feature = resolve_feature(root, feature_raw)
    contract = load_json(contract_path(feature))
    context = load_json(context_path(feature)) if os.path.isfile(context_path(feature)) else {}
    completed, open_slices, current = parse_tasks(feature)
    verifier_candidates = [
        os.path.join(feature, "verification.json"),
        os.path.join(feature, "evidence", "v2-verifier.json"),
        os.path.join(feature, "evidence", "verifier.json"),
        os.path.join(root, ".specify", "eval", "last.json"),
    ]
    verifier = next((path for path in verifier_candidates if os.path.isfile(path)), None)
    raw_verification = load_json(verifier) if verifier else None
    last = None
    if raw_verification is not None:
        results = raw_verification.get("results") or raw_verification.get("checks") or []
        failed = []
        for item in results:
            item_status = item.get("status") or item.get("verdict")
            if item_status not in (None, "PASS", "pass") or item.get("exit_code", 0) != 0:
                failed.append(item.get("id") or item.get("name") or "unknown")
        last = {
            "source": rel(root, verifier),
            "profile": raw_verification.get("profile"),
            "verdict": raw_verification.get("verdict") or raw_verification.get("status"),
            "exit_code": raw_verification.get("exit_code"),
            "check_count": len(results),
            "failed_checks": failed,
            "environment": raw_verification.get("environment") or {},
        }
    if status is None:
        status = (
            "done" if not open_slices and completed else ("blocked" if blockers else "executing")
        )
    value = {
        "schema_version": "1.0",
        "work_id": contract.get("id"),
        "status": status,
        "current_slice": current,
        "context_pack_hash": context.get("content_hash") or "",
        "completed_slices": completed,
        "open_slices": open_slices,
        "last_verification": last,
        "next_action": next_action
        or ("review/converge" if not open_slices else f"execute {current}"),
        "blockers": list(blockers or []),
        "git_checkpoint": git(root, ["rev-parse", "HEAD"]) or "WORKTREE",
    }
    write_json(handoff_path(feature), value)
    return value


# ---------------------------------------------------------------------------
# Shared path helpers for Documentation Freshness / Repository Gardening
# ---------------------------------------------------------------------------


def norm_rel(path):
    path = (path or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def path_matches(path, pattern):
    """repository-relative path 를 glob pattern 하나에 맞춰본다.

    fnmatch 는 '**' 를 모른다. policy 에서 실제로 쓰는 세 형태만 직접 처리한다.
    """
    path = norm_rel(path)
    pattern = norm_rel(pattern)
    if not path or not pattern:
        return False
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/"):
        tail = pattern[3:]
        if fnmatch.fnmatchcase(path, tail) or fnmatch.fnmatchcase(os.path.basename(path), tail):
            return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def matches_any(path, patterns):
    return any(path_matches(path, pattern) for pattern in patterns or [])


expand_repo_target = amplai_docs.expand_repo_target


declared_directory_paths = amplai_docs.declared_directory_paths


dependency_path_record = amplai_docs.dependency_path_record


extract_typed_references = amplai_docs.extract_typed_references


reference_local_roots = amplai_docs.reference_local_roots


classify_document_reference = amplai_docs.classify_document_reference


def documentation_base_commit(root, feature, contract):
    """Keep a Work's original baseline when its changes are committed or main advances."""
    target = doc_impact_path(feature)
    if os.path.isfile(target):
        previous = load_json(target)
        if previous.get("content_hash") == object_sha(previous) and previous.get(
            "work_id"
        ) == contract.get("id"):
            base = (previous.get("dependency_snapshot") or {}).get("base_commit")
            if base:
                strict_git(root, ["cat-file", "-e", base + "^{commit}"])
                return base
    context_file = context_path(feature)
    if os.path.isfile(context_file):
        context = load_json(context_file)
        base = (context.get("generated_from") or {}).get("commit")
        if base and base not in PROVENANCE_MARKERS:
            strict_git(root, ["cat-file", "-e", base + "^{commit}"])
            return base
    return work_base_commit(root)


def meaningful_document_content(content, path):
    if path.endswith(".md"):
        text = content.decode("utf-8")
        metadata = re.compile(
            r"^\s*(?:>\s*)?(?:\*\*)?(?:last[_ -]?(?:reviewed|updated|modified)|"
            r"reviewed(?:[_ -]?(?:at|by|date))?|review[_ -]?date|updated(?:[_ -]?at)?)"
            r"(?:\*\*)?\s*:\s*.*$",
            re.IGNORECASE,
        )
        frontmatter_date = re.compile(r"^\s*(?:date|created(?:[_ -]?at)?)\s*:", re.IGNORECASE)
        lines = []
        frontmatter = False
        for index, line in enumerate(text.splitlines()):
            if line.strip() == "---":
                frontmatter = index == 0
            if metadata.match(line) or (frontmatter and frontmatter_date.match(line)):
                continue
            lines.append(line)
        text = "\n".join(lines)
        content = text.encode("utf-8")
    return re.sub(rb"\s+", b"", content)


def collect_changed_sources(root, base=None):
    return amplai_docs.changed_paths(root, base)


def strict_git(root, args, empty_codes=()):
    return amplai_docs.strict_git(root, args, empty_codes=empty_codes) or b""


def git_paths(root, args):
    return amplai_docs.git_paths(root, args)


def split_semantic_changes(paths, policy):
    """의미 변경과 그렇지 않은 변경을 나눈다.

    formatting/comment/문서만 바뀐 Work 에서 documentation impact 를 강제하지
    않으려면 이 구분이 먼저 있어야 한다.
    """
    semantic = []
    other = []
    include = policy.get("semantic_change_paths") or []
    exclude = policy.get("non_semantic_change_paths") or []
    for path in paths:
        # A document/metadata edit is semantic. New unmatched tooling is also
        # semantic by default; an old blanket docs/** exclusion cannot hide it.
        if path.endswith((".md", amplai_docs.SIDECAR)) or matches_any(path, include):
            semantic.append(path)
        elif matches_any(path, exclude):
            other.append(path)
        else:
            semantic.append(path)
    return semantic, other


# ---------------------------------------------------------------------------
# Documentation Freshness
# ---------------------------------------------------------------------------


PATH_IN_CODE = re.compile(r"`([^`\n]+)`")
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
TREE_ROOT = re.compile(r"^([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/)$")
TREE_ITEM = re.compile(r"^([\s│|]*)[├└]──[ \t]+(.+?)\s*$")
GLOB_CHARS = "*?[]"


def doc_impact_path(feature):
    return os.path.join(feature, "doc-impact.json")


def garden_report_path(feature):
    return os.path.join(feature, "garden-report.json")


clean_declared_path = amplai_docs.clean_declared_path


looks_like_repo_path = amplai_docs.looks_like_repo_path


extract_declared_paths = amplai_docs.extract_declared_paths


reference_candidates = amplai_docs.reference_candidates


reference_exists = amplai_docs.reference_exists


scan_broken_references = amplai_docs.scan_broken_references


docs_referencing = amplai_docs.docs_referencing


document_category = amplai_docs.document_category


def discover_impacted_documents(root, policy, semantic_paths, contract=None):
    """deterministic evidence 만으로 impacted document 후보를 만든다.

    '관련 문서를 알아서 찾아라' 로 시작하지 않는다. 다섯 축에서 나온 후보에만
    의미 판단을 붙인다.
    """
    found = {}

    def add(doc, axis, reason, evidence=None):
        doc = norm_rel(doc or "")
        amplai_docs.relative_path(doc)
        if not os.path.isfile(amplai_docs.safe_path(root, doc)):
            raise amplai_docs.DocumentError("MISSING_SOURCE")
        item = found.setdefault(doc, {"axes": [], "reasons": [], "evidence": []})
        if axis not in item["axes"]:
            item["axes"].append(axis)
        if reason and reason not in item["reasons"]:
            item["reasons"].append(reason)
        for value in evidence or []:
            if value not in item["evidence"]:
                item["evidence"].append(value)

    for rule in policy.get("impact_rules") or []:
        if not any(matches_any(path, rule.get("when_changed") or []) for path in semantic_paths):
            continue
        for target in rule.get("impacted") or []:
            for path in expand_repo_target(root, target):
                add(
                    path,
                    "impact_rules",
                    rule.get("reason") or rule.get("id"),
                    [".ai-team/policy/documentation.json#{}".format(rule.get("id"))],
                )

    # ontology binding 축은 semantic runtime 과 함께 이식하지 않았다 (D-046).
    # 나머지 네 축(impact_rules, knowledge_map, decision_index, doc_reference)이 후보를 좁힌다.

    with amplai_docs.SourceTree(root) as tree:
        knowledge = amplai_docs.strict_json(tree.read(KNOWLEDGE_MAP))
        decisions = amplai_docs.strict_json(tree.read(DECISIONS))
    if (
        not isinstance(knowledge, dict)
        or not isinstance(knowledge.get("sources"), list)
        or not isinstance(decisions, dict)
        or not isinstance(decisions.get("entries"), list)
    ):
        raise amplai_docs.DocumentError("INVALID_KNOWLEDGE_INDEX")
    exclude = set((knowledge.get("selection") or {}).get("exclude_status") or [])
    stopwords = {value.lower() for value in policy.get("keyword_stopwords") or []}
    # keyword 를 substring 으로 맞추면 'cortex' 같은 전역 단어가 모든 문서를 끌어온다.
    # path 를 segment 로 쪼개 정확히 일치할 때만 신호로 본다.
    segments = set()
    for path in semantic_paths:
        for part in re.split(r"[/._\-]", path.lower()):
            if len(part) >= 3:
                segments.add(part)
    segments -= stopwords
    for item in knowledge.get("sources") or []:
        if item.get("status") in exclude:
            continue
        target = norm_rel(item.get("path") or "")
        if not target.endswith(".md"):
            continue
        hits = [keyword for keyword in item.get("keywords") or [] if keyword.lower() in segments]
        if hits:
            add(
                target,
                "knowledge_map",
                "keyword {}".format(",".join(sorted(hits)[:3])),
                [".ai-team/knowledge/map.json#{}".format(item.get("id"))],
            )

    for entry in decisions.get("entries") or []:
        if entry.get("status") != "active":
            continue
        scopes = [norm_rel(value) for value in entry.get("scope") or []]
        related = any(
            path == scope or path.startswith(scope.rstrip("/") + "/")
            for path in semantic_paths
            for scope in scopes
            if scope
        )
        if not related:
            continue
        add(
            entry.get("path"),
            "decision_index",
            "active decision {}".format(entry.get("id")),
            [".ai-team/knowledge/decisions.index.json#{}".format(entry.get("id"))],
        )

    # Work reports are already excluded from the resulting current-guide set.
    # Apply that same policy before parsing literal diagnostic HTML in them.
    for doc, refs in docs_referencing(
        root, semantic_paths, include_document=lambda path: not work_scoped_artifact(policy, path)
    ).items():
        add(
            doc,
            "doc_reference",
            "문서가 {} 를 직접 언급".format(", ".join(sorted(refs)[:3])),
            [f"git grep -F {sorted(refs)[0]}"],
        )

    return found


def work_scoped_artifact(policy, path):
    """Work memory 인지 본다.

    spec/plan/tasks 는 그 Work 의 기록이지 canonical 문서가 아니다. 끝난 Work 의
    것을 고치면 이력을 덮어쓰고, 진행 중인 Work 의 것은 impact 대상이 아니라
    산출물 자체다.
    """
    for item in policy.get("canonical_roots") or []:
        if item.get("kind") != "work_scoped":
            continue
        prefix = norm_rel(item.get("path") or "").rstrip("/")
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            return True
    return False


def work_base_commit(root):
    """이 Work 가 갈라져 나온 지점.

    HEAD 대비 diff 만 보면 이미 commit 한 문서 갱신이 '미처리' 로 보인다.
    documentation impact 는 Work 전체를 대상으로 판정해야 한다.
    """
    for ref in ("origin/main", "main", "origin/master", "master"):
        base = git(root, ["merge-base", "HEAD", ref])
        if base:
            return base
    return ""


def docs_impact(root, feature_raw, changed=None, write=True, acknowledged=None):
    return amplai_docs.impact(
        root, resolve_feature(root, feature_raw), globals(), changed, write, acknowledged
    )


def docs_review_batch(root, feature_raw, request):
    return amplai_docs.review_batch(root, resolve_feature(root, feature_raw), request, globals())


def docs_review(
    root, feature_raw, document, outcome, reason, reviewer, evidence, expected_snapshot, method=None
):
    return docs_review_batch(
        root,
        feature_raw,
        {
            "snapshot": expected_snapshot,
            "reference_reviews": [],
            "reviews": [
                {
                    "document": document,
                    "outcome": outcome,
                    "reason": reason,
                    "reviewer": reviewer,
                    "method": method,
                    "evidence": evidence,
                }
            ],
        },
    )


def validate_decision_history(root, policy):
    """superseded decision 이 이력을 유지하는지 본다. 덮어쓰기를 막는 검사다."""
    errors = []
    checked = 0
    try:
        index = load_json(os.path.join(root, DECISIONS))
    except Exception as exc:
        return {"errors": [f"decision index 를 읽을 수 없음: {exc}"], "checked": 0}
    for entry in index.get("entries") or []:
        checked += 1
        if entry.get("status") == "superseded" and not entry.get("superseded_by"):
            errors.append(
                "superseded decision 에 superseded_by 가 없다: {}".format(entry.get("id"))
            )
        target = norm_rel(entry.get("path") or "")
        if target and not os.path.exists(os.path.join(root, target)):
            errors.append(
                "decision index 가 없는 문서를 가리킨다: {} -> {}".format(entry.get("id"), target)
            )
    for item in policy.get("historical_documents") or []:
        path = norm_rel(item.get("path") or "")
        if path and not os.path.isfile(os.path.join(root, path)):
            errors.append(f"historical decision 문서가 없다: {path}")
    return {"errors": errors, "checked": checked}


def docs_validate(root, feature_raw=None, repo=False):
    policy = load_json(os.path.join(root, DOCUMENTATION_POLICY))
    if repo:
        broken = scan_broken_references(root, policy)
        history = validate_decision_history(root, policy)
        errors = ["unresolved repository reference" for _ in broken] + history["errors"]
        return {
            "valid": not errors,
            "verdict": "FRESH" if not errors else "STALE",
            "mode": "repository",
            "broken_references": broken,
            "decision_history": history,
            "errors": errors,
        }
    try:
        current = docs_impact(root, feature_raw, write=False)
        path = doc_impact_path(resolve_feature(root, feature_raw))
        previous = load_json(path)
        errors = []
        if previous.get("content_hash") != object_sha(previous):
            errors.append("INVALID_IMPACT_REPORT")
        if previous.get("dependency_snapshot_hash") != current["dependency_snapshot_hash"]:
            errors.append("SOURCE_DRIFT")

        def comparable(value):
            # Commit ids are provenance: the shipped report must not drift merely because
            # its own commit moved HEAD. Content digests still drive SOURCE_DRIFT above.
            result = {}
            for key, item in value.items():
                if key in ("content_hash", "acknowledged"):
                    continue
                if key == "generated_from" and isinstance(item, dict):
                    item = {k: v for k, v in item.items() if k != "commit"}
                if key == "impacted_documents" and isinstance(item, list):
                    item = [
                        {
                            **doc,
                            "record_snapshot": {
                                k: v
                                for k, v in (doc.get("record_snapshot") or {}).items()
                                if k != "candidate_revision"
                            },
                        }
                        if isinstance(doc, dict)
                        else doc
                        for doc in item
                    ]
                result[key] = item
            return result

        if comparable(previous) != comparable(current):
            errors.append("DERIVED_REPORT_DRIFT")
        if not current["scan_complete"] or current["status"] == "STALE":
            errors.append("DOCUMENT_REVIEW_REQUIRED")
        return {
            "valid": not errors,
            "verdict": current["status"] if not errors else "STALE",
            "mode": "work",
            "errors": errors,
            "dependency_snapshot_hash": current["dependency_snapshot_hash"],
        }
    except (OSError, ValueError, RuntimeError):
        return {
            "valid": False,
            "verdict": "INCOMPLETE",
            "mode": "work",
            "errors": ["CURRENT_INPUT_OR_EVIDENCE_UNAVAILABLE"],
        }


# ---------------------------------------------------------------------------
# Repository Gardening
# ---------------------------------------------------------------------------


SAFE_TYPES = ()  # A filename or untracked status proves neither ownership nor regeneration.


def git_lines(root, args):
    return [norm_rel(line) for line in git(root, args).splitlines() if line.strip()]


def repo_file_sets(root):
    """Complete NUL-delimited inventories; unavailable Git is never an empty scan."""
    tracked = set(amplai_docs.git_paths(root, ["ls-files", "-z"]))
    untracked = set(
        amplai_docs.git_paths(root, ["ls-files", "--others", "--exclude-standard", "-z"])
    )
    ignored = set(
        amplai_docs.git_paths(
            root, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"]
        )
    )
    return tracked, untracked, ignored


def classify_target(policy, path):
    patterns = policy.get("classification_patterns") or {}
    return {
        "public_api": matches_any(path, patterns.get("public_api")),
        "conditional_build": matches_any(path, patterns.get("conditional_build")),
        "plugin_entry": matches_any(path, patterns.get("plugin_entry")),
        "production_path": matches_any(path, patterns.get("production_path")),
    }


def safety_for(policy, path, candidate_type, tracked):
    """삭제 안전 단계를 정한다.

    HUMAN_GATED 판정이 가장 먼저다. Cortex 의 legacy/compatibility 자산은
    dead 처럼 보여도 자동으로 지우지 않는다.
    """
    classification = classify_target(policy, path)
    if candidate_type in ("temporary_backup", "prunable_worktree") or (
        tracked and path.lower().endswith(".md")
    ):
        return "HUMAN_GATED", classification
    if matches_any(path, policy.get("human_gated_paths")):
        return "HUMAN_GATED", classification
    if any(classification.values()):
        return "HUMAN_GATED", classification
    if candidate_type in SAFE_TYPES and not tracked:
        return "SAFE_AUTO", classification
    return "EVIDENCE_REQUIRED", classification


def make_candidate(policy, index, candidate_type, path, reason, evidence, tracked, symbol=None):
    safety, classification = safety_for(policy, path, candidate_type, tracked)
    risk = {"SAFE_AUTO": "low", "EVIDENCE_REQUIRED": "medium", "HUMAN_GATED": "high"}[safety]
    action = {"SAFE_AUTO": "review", "EVIDENCE_REQUIRED": "review", "HUMAN_GATED": "propose"}[
        safety
    ]
    item = {
        "candidate_id": "G-%03d" % index,
        "type": candidate_type,
        "target": {"path": path, "symbol": symbol},
        "reason": reason,
        "evidence": list(evidence or []),
        "risk": risk,
        "classification": classification,
        "safety": safety,
        "recommended_action": action,
        "status": "candidate",
    }
    if safety == "HUMAN_GATED":
        item["gate"] = (
            (policy.get("safety_levels") or {})
            .get("HUMAN_GATED", {})
            .get("gate", "destructive_change")
        )
    return item


def in_scope(path, scope):
    if not scope:
        return False
    for entry in scope:
        entry = norm_rel(entry)
        if entry == ".":
            return True
        if fnmatch.fnmatchcase(path, entry):
            return True
        if path == entry or path.startswith(entry.rstrip("/") + "/"):
            return True
    return False


def garden_scope(root, contract, mode):
    if mode == "full":
        return ["."]
    scope = (contract.get("scope") or {}).get("include") or []
    if not isinstance(scope, list) or any(not isinstance(p, str) for p in scope):
        raise ValueError("INVALID_GARDEN_SCOPE")
    for path in scope:
        amplai_docs.relative_path(path.rstrip("/"))
        if path in ("*", "**", "**/*"):
            raise ValueError("EXPLICIT_GARDENING_WORK_REQUIRED")
    # Other developers' dirty files and parent directories cannot broaden this Work.
    return sorted(set(scope))


def reference_terms(path):
    """이 파일을 가리킬 수 있는 검색어를 만든다.

    Python module 은 'from .semantic_ast import ...' 처럼 확장자 없이 참조된다.
    basename 만 찾으면 살아 있는 module 을 orphan 이라고 부르게 된다.
    """
    name = os.path.basename(path)
    terms = [name]
    stem, ext = os.path.splitext(name)
    if ext == ".py" and stem not in ("__init__", "__main__"):
        terms.append(stem)
    return [term for term in terms if len(term) >= 5]


def unreferenced_paths(root, paths, limit, exclude=None):
    """어떤 검색어로도 참조되지 않는 파일을 고른다.

    참조가 0 이라는 사실 하나로 삭제 근거를 삼지 않는다. 이 결과는
    EVIDENCE_REQUIRED candidate 의 출발점일 뿐이다.

    exclude 는 '참조로 세지 않을' 경로다. Work artifact 는 조사 기록이라
    repository 경로를 대량 나열한다 — 그것을 참조로 세면 아무것도 orphan 이
    되지 않는다.
    """
    excluded = [":(exclude)" + norm_rel(item) for item in exclude or []]
    results = []
    truncated = False
    for index, path in enumerate(paths):
        if index >= limit:
            truncated = True
            break
        terms = reference_terms(path)
        if not terms:
            continue
        referenced = False
        for term in terms:
            try:
                subprocess.check_output(
                    [
                        *amplai_docs.GIT_READ_PREFIX,
                        "grep",
                        "-l",
                        "-I",
                        "-F",
                        term,
                        "--",
                        ".",
                        ":(exclude)" + path,
                        *excluded,
                    ],
                    cwd=root,
                    stderr=subprocess.STDOUT,
                )
                referenced = True
                break
            except subprocess.CalledProcessError as exc:
                if exc.returncode == 1:
                    continue
                raise ValueError("REFERENCE_SCAN_UNAVAILABLE") from None
            except (OSError, subprocess.TimeoutExpired):
                raise ValueError("REFERENCE_SCAN_UNAVAILABLE") from None
        if not referenced:
            results.append(path)
    return results, truncated


def prunable_worktrees(root):
    values = []
    text = git(root, ["worktree", "list", "--porcelain"])
    current = {}
    for line in [*text.splitlines(), ""]:
        if not line.strip():
            if current.get("worktree") and current.get("prunable"):
                values.append(current)
            current = {}
            continue
        if " " in line:
            key, value = line.split(" ", 1)
        else:
            key, value = line, ""
        current[key] = value or True
    return values


def collect_garden_candidates(root, policy, scope, mode):
    tracked, untracked, ignored = repo_file_sets(root)
    safe_patterns = (
        policy.get("generated_candidate_patterns") or policy.get("safe_auto_patterns") or []
    ) + (policy.get("user_backup_patterns") or [])
    limits = policy.get("scan_limits") or {}
    limit = (
        limits.get("full_reference_scan" if mode == "full" else "incremental_reference_scan") or 120
    )
    candidates = []
    notes = []
    index = 1

    # 1. generated garbage / temporary backup — git 이 추적하지 않는 것만 본다.
    loose = sorted(untracked | ignored)
    for path in loose:
        if not in_scope(path, scope) or not matches_any(path, safe_patterns):
            continue
        lowered = path.lower()
        if "__pycache__" in lowered or lowered.endswith((".pyc", ".pyo")):
            kind = "generated_garbage"
        else:
            kind = "temporary_backup"
        candidates.append(
            make_candidate(
                policy,
                index,
                kind,
                path,
                f"git 이 추적하지 않는 {kind} 패턴 artifact",
                ["git ls-files --others", ".ai-team/policy/gardening.json#safe_auto_patterns"],
                tracked=False,
            )
        )
        index += 1

    # 2. prunable worktree
    for item in prunable_worktrees(root) if mode == "full" else []:
        path = norm_rel(str(item.get("worktree") or ""))
        candidates.append(
            make_candidate(
                policy,
                index,
                "prunable_worktree",
                path,
                "git worktree 가 prunable 로 표시한다",
                ["git worktree list --porcelain"],
                tracked=False,
            )
        )
        index += 1

    # 3. broken documentation reference
    try:
        doc_policy = load_json(os.path.join(root, DOCUMENTATION_POLICY))
        for finding in scan_broken_references(root, doc_policy):
            if not in_scope(finding["document"], scope):
                continue
            candidates.append(
                make_candidate(
                    policy,
                    index,
                    "broken_reference",
                    finding["document"],
                    "선언된 경로 {} 가 없다".format(finding["reference"]),
                    [
                        "{}:{}".format(finding["document"], finding["line"]),
                        ".ai-team/policy/documentation.json#reference_scan",
                    ],
                    tracked=finding["document"] in tracked,
                )
            )
            index += 1
    except Exception:
        notes.append("INCOMPLETE:DOCUMENT_REFERENCE_SCAN_UNAVAILABLE")

    # 4. orphan test fixture / obsolete script — 참조 0 은 근거의 시작일 뿐이다.
    #    runner 가 glob/디렉토리로 수집하는 entry 는 이름 참조가 없는 것이 정상이라
    #    후보에서 뺀다. 그러지 않으면 살아 있는 test 를 orphan 이라고 부르게 된다.
    extensions = tuple(policy.get("reference_scan_extensions") or [])
    discovery_patterns = [
        item.get("pattern")
        for item in policy.get("discovery_entry_patterns") or []
        if item.get("pattern")
    ]
    considered = [
        path
        for path in sorted(tracked)
        if in_scope(path, scope)
        and path.endswith(extensions)
        and (path.startswith("tests/") or path.startswith("tools/") or path.startswith("scripts/"))
    ]
    scannable = [path for path in considered if not matches_any(path, discovery_patterns)]
    skipped = len(considered) - len(scannable)
    if skipped:
        notes.append(
            "discovery entry %d 개는 orphan 판정에서 제외했다 (runner 가 glob/디렉토리로 수집)"
            % skipped
        )
    orphans, truncated = unreferenced_paths(
        root, scannable, limit, exclude=policy.get("reference_scan_exclude")
    )
    if truncated:
        notes.append(
            "INCOMPLETE:reference scan 상한 %d 개에 도달해 %d 개 파일을 검사하지 않았다"
            % (limit, max(0, len(scannable) - limit))
        )
    for path in orphans:
        kind = "orphan_test_fixture" if path.startswith("tests/") else "obsolete_script"
        candidates.append(
            make_candidate(
                policy,
                index,
                kind,
                path,
                "repository 안에서 이 파일 이름을 참조하는 곳이 없다",
                [
                    f"git grep -F {os.path.basename(path)}",
                    ".ai-team/policy/gardening.json#no_static_reference_is_not_dead",
                ],
                tracked=True,
            )
        )
        index += 1

    return candidates, notes


def garden_integrity(candidates, applied_paths):
    violations = []
    all_classified = True
    all_have_evidence = True
    for item in candidates:
        if not item.get("safety") or not isinstance(item.get("classification"), dict):
            all_classified = False
            violations.append("classification 없음: {}".format(item.get("candidate_id")))
        if not item.get("evidence"):
            all_have_evidence = False
            violations.append("evidence 없음: {}".format(item.get("candidate_id")))
    gated_applied = [
        item.get("candidate_id")
        for item in candidates
        if item.get("safety") != "SAFE_AUTO" and item.get("status") == "applied"
    ]
    for candidate_id in gated_applied:
        violations.append(f"SAFE_AUTO 가 아닌 candidate 가 삭제됨: {candidate_id}")
    if applied_paths or any(item.get("status") == "applied" for item in candidates):
        violations.append("UNVERIFIED_GARDEN_MUTATION")
    return {
        "all_classified": all_classified,
        "all_have_evidence": all_have_evidence,
        "no_gated_auto_delete": not gated_applied,
        "violations": violations,
    }


def build_garden_report(root, policy, mode, scope, candidates, applied_paths, notes, work_id=None):
    by_safety = {}
    by_type = {}
    for item in candidates:
        by_safety[item["safety"]] = by_safety.get(item["safety"], 0) + 1
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1
    integrity = garden_integrity(candidates, applied_paths)
    tree = worktree_state(root)
    value = {
        "schema_version": "1.0",
        "work_id": work_id,
        "mode": mode,
        "applied": bool(applied_paths),
        "scope": list(scope),
        "candidates": candidates,
        "summary": {
            "total": len(candidates),
            "by_safety": by_safety,
            "by_type": by_type,
            "applied_count": len(applied_paths),
            "gated_count": by_safety.get("HUMAN_GATED", 0),
        },
        "integrity": integrity,
        "applied_paths": list(applied_paths),
        "notes": notes,
        "report_only": True,
        "scan_complete": not any(note.startswith("INCOMPLETE:") for note in notes),
        "pass": not integrity["violations"]
        and not any(note.startswith("INCOMPLETE:") for note in notes),
        "generated_from": {
            "commit": tree["commit"],
            "worktree_state": tree["state"],
            "worktree_dirty": tree["dirty"],
            "generated_at": utc_now(),
            "policy_sha256": file_sha(os.path.join(root, GARDENING_POLICY)),
        },
        "rule": policy.get("rule") or "proposal only",
    }
    value["content_hash"] = object_sha(value)
    return value


def garden_incremental(root, feature_raw, write=True):
    feature = resolve_feature(root, feature_raw)
    contract = load_json(contract_path(feature))
    policy = load_json(os.path.join(root, GARDENING_POLICY))
    scope = garden_scope(root, contract, "incremental")
    candidates, notes = collect_garden_candidates(root, policy, scope, "incremental")
    value = build_garden_report(
        root, policy, "incremental", scope, candidates, [], notes, contract.get("id")
    )
    if write:
        write_json(garden_report_path(feature), value)
    return value


def garden_full(root, report_only=True, output=None, feature_raw=None):
    if not feature_raw:
        raise ValueError("EXPLICIT_GARDENING_WORK_REQUIRED")
    feature = resolve_feature(root, feature_raw)
    contract = load_json(contract_path(feature))
    if contract.get("work_type") != "repository_gardening" or contract.get("risk") != "high":
        raise ValueError("EXPLICIT_GARDENING_WORK_REQUIRED")
    if not report_only:
        raise ValueError("APPROVAL_REQUIRED")
    policy = load_json(os.path.join(root, GARDENING_POLICY))
    scope = ["."]
    candidates, notes = collect_garden_candidates(root, policy, scope, "full")
    applied = []
    value = build_garden_report(
        root, policy, "full", scope, candidates, applied, notes, contract.get("id")
    )
    if output:
        write_json(safe_path(root, output), value)
    return value


def apply_safe_candidates(root, candidates):
    """Compatibility entry: classification is a report, never deletion authority."""
    for item in candidates:
        item["status"] = "gated" if item.get("safety") == "HUMAN_GATED" else "kept"
    return []


def garden_apply(root, feature_raw, write=True):
    feature = resolve_feature(root, feature_raw)
    contract = load_json(contract_path(feature))
    policy = load_json(os.path.join(root, GARDENING_POLICY))
    scope = garden_scope(root, contract, "incremental")
    candidates, notes = collect_garden_candidates(root, policy, scope, "incremental")
    applied = apply_safe_candidates(root, candidates)
    value = build_garden_report(
        root, policy, "incremental", scope, candidates, applied, notes, contract.get("id")
    )
    if write:
        write_json(garden_report_path(feature), value)
    return value


def quadrant_audit(root):
    policy = load_json(os.path.join(root, QUADRANT_POLICY))
    result = []
    for name, item in sorted((policy.get("quadrants") or {}).items()):
        assets = item.get("required_assets") or []
        missing = [path for path in assets if not os.path.exists(os.path.join(root, path))]
        result.append({"quadrant": name, "assets": assets, "missing": missing, "pass": not missing})
    return {"pass": all(item["pass"] for item in result), "quadrants": result}


def garden_scan(root):
    findings = []
    claims = read_jsonl(os.path.join(root, CLAIMS))
    ids = {}
    for item in claims:
        claim_id = item.get("id")
        if claim_id in ids:
            findings.append({"type": "duplicate_claim_id", "id": claim_id, "severity": "block"})
        ids[claim_id] = item
        if item.get("status") == "active" and not item.get("evidence"):
            findings.append({"type": "missing_evidence", "id": claim_id, "severity": "warn"})
        for evidence in item.get("evidence") or []:
            path = evidence.get("path") or evidence.get("source")
            # git-history 는 파일이 아니라 "지금은 없고 history 에만 있다"는 marker 다.
            # superseded claim 의 근거는 삭제된 파일인 게 정상이라 이걸 stale 로 세지 않는다.
            if path in EVIDENCE_MARKERS:
                continue
            if path and not os.path.exists(os.path.join(root, path)):
                findings.append(
                    {
                        "type": "stale_evidence_path",
                        "id": claim_id,
                        "path": path,
                        "severity": "warn",
                    }
                )
        if item.get("status") == "superseded" and not item.get("superseded_by"):
            findings.append(
                {"type": "superseded_without_target", "id": claim_id, "severity": "warn"}
            )
    # ontology local-name 검사는 semantic runtime 과 함께 이식하지 않았다 (D-046).
    # 이 저장소에는 RDF graph 가 없고 Knowledge Vault 와 Proposal 모델이 그 자리를
    # 대신한다. 같은 층의 검사는 verifier registry 의 vault-lint 가 맡는다.
    return {
        "pass": not any(item.get("severity") == "block" for item in findings),
        "finding_count": len(findings),
        "findings": findings,
        "rule": "proposal only; harness/ontology를 자동 수정하지 않는다",
    }

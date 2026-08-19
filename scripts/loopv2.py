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
import shutil
import subprocess
import sys

READINESS_POLICY = os.path.join(".ai-team", "policy", "knowledge-readiness.json")
PERMISSION_POLICY = os.path.join(".ai-team", "policy", "permissions.json")
QUADRANT_POLICY = os.path.join(".ai-team", "policy", "quadrant.json")
DOCUMENTATION_POLICY = os.path.join(".ai-team", "policy", "documentation.json")
GARDENING_POLICY = os.path.join(".ai-team", "policy", "gardening.json")
KNOWLEDGE_MAP = os.path.join(".ai-team", "knowledge", "map.json")
CLAIMS = os.path.join(".ai-team", "knowledge", "claims.jsonl")
DECISIONS = os.path.join(".ai-team", "knowledge", "decisions.index.json")
ONTOLOGY_BINDINGS = os.path.join("docs", "ontology", "bindings", "cortex.yaml")

# 파일 경로가 아닌 evidence locator. superseded claim 의 근거는 이미 삭제된
# 파일인 게 정상이므로 이런 marker 를 stale 로 세지 않는다.
EVIDENCE_MARKERS = ("git-history",)


def utc_now():
    return (
        datetime.datetime.now(datetime.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


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
        out = subprocess.check_output(["git", *list(args)], cwd=root, stderr=subprocess.STDOUT)
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
        subprocess.check_output(["git", *list(args)], cwd=root, stderr=subprocess.STDOUT)
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


def ontology_candidates(root, goal):
    script_dir = os.path.join(root, "tools", "ontology")
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    try:
        from semantic_runtime import SemanticRuntime

        runtime = SemanticRuntime(root=root)
        return runtime.search(goal, 20)
    except Exception as exc:
        return [
            {"uri": None, "name": "semantic-runtime-unavailable", "score": 0, "error": str(exc)}
        ]


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
        "ontology_candidates": ontology_candidates(root, contract.get("goal") or " ".join(terms)),
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
        if item.get("status") in exclude:
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
        if (item.get("priority") or 0) >= 90 or hits:
            selected = copy.deepcopy(item)
            selected["score"] = round(score, 4)
            selected["reason"] = (
                "keyword:{}".format(",".join(hits)) if hits else "high-priority-core"
            )
            values.append(selected)
    values.sort(
        key=lambda item: (-item["score"], -(item.get("priority") or 0), item.get("id") or "")
    )
    return values[:limit]


def active_claims(root, contract):
    scope = (contract.get("scope") or {}).get("include") or []
    values = []
    for item in read_jsonl(os.path.join(root, CLAIMS)):
        if item.get("status") in ("deprecated", "superseded", "rejected"):
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
    scope = (contract.get("scope") or {}).get("include") or []
    result = []
    for item in value.get("entries") or []:
        if item.get("status") != "active":
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
    sources = selected_sources(root, contract)
    claims = active_claims(root, contract)
    decisions = active_decisions(root, contract)
    code_scope = list((contract.get("scope") or {}).get("include") or [])
    tests = derive_test_scope(root, code_scope)
    semantic = ontology_candidates(root, contract.get("goal") or "")
    required_knowledge = []
    for item in sources:
        path = item.get("path")
        required_knowledge.append(
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "path": path,
                "reason": item.get("reason"),
                "source_sha256": file_sha(os.path.join(root, path)) if path else "",
                "evidence": [],
            }
        )
    for item in claims:
        path = item.get("evidence", [{}])[0].get("path", CLAIMS)
        required_knowledge.append(
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "path": path,
                "reason": "active claim",
                "source_sha256": file_sha(os.path.join(root, path)) if path else "",
                "evidence": item.get("evidence") or [],
            }
        )
    ontology_refs = []
    for item in semantic:
        if item.get("uri"):
            ontology_refs.append(
                {
                    "id": item.get("uri"),
                    "status": "active",
                    "path": "docs/ontology/manifest.json",
                    "reason": "semantic search score={}".format(item.get("score")),
                    "evidence": [],
                }
            )
    tree = worktree_state(root)
    generated = {
        "commit": tree["commit"],
        "worktree_state": tree["state"],
        "worktree_dirty": tree["dirty"],
        "generated_at": utc_now(),
        "contract_sha256": file_sha(contract_file),
        "knowledge_map_sha256": file_sha(os.path.join(root, KNOWLEDGE_MAP)),
        "claims_sha256": file_sha(os.path.join(root, CLAIMS)),
        "readiness_sha256": file_sha(readiness_file),
        "discovery_sha256": file_sha(discovery_path(feature)),
    }
    value = {
        "schema_version": "1.0",
        "work_id": contract.get("id"),
        "goal": contract.get("goal"),
        "knowledge_verdict": verdict,
        "required_knowledge": required_knowledge,
        "active_decisions": [
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "path": item.get("path"),
                "reason": item.get("title"),
                "evidence": [],
            }
            for item in decisions
        ],
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
    commit = generated.get("commit")
    if commit and not git_commit_exists(root, commit):
        errors.append(f"provenance commit이 이 repository에 없음: {commit}")

    # source 상태: 기록된 hash와 현재 파일을 다시 비교해 stale을 잡는다.
    sources = []
    stale = []
    missing = []
    superseded = []
    for item in value.get("required_knowledge") or []:
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
        "tools": [
            command_version(["python3", "--version"], root),
            command_version(["cc", "--version"], root),
            command_version(["make", "--version"], root),
            command_version(["git", "--version"], root),
        ],
        "target_assumptions": {
            "c_standard": "C99",
            "architectures": ["x86-32", "x86_64"],
            "operating_systems": ["RHEL5", "RHEL7", "RHEL8", "HP-UX where guarded"],
            "python_compatibility": ["Python 3.6 for deployed operational scripts when applicable"],
        },
        "fixture_refs": [
            "testdata/",
            "docs/ontology/tests/fixtures/",
        ],
        "safe_environment": {
            key: os.environ.get(key)
            for key in ("CC", "CFLAGS", "PYTHONPATH", "CORTEX_PYTHON3")
            if os.environ.get(key)
        },
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


def collect_changed_sources(root, base=None):
    """이번 Work 가 실제로 건드린 repository-relative 경로를 모은다."""
    paths = set()
    sources = [["diff", "--name-only", "HEAD"]]
    if base:
        sources.append(["diff", "--name-only", base, "HEAD"])
    for args in sources:
        for line in git(root, args).splitlines():
            if line.strip():
                paths.add(norm_rel(line))
    for line in git(root, ["status", "--porcelain=v1", "--untracked-files=all"]).splitlines():
        if len(line) < 4:
            continue
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        paths.add(norm_rel(value))
    return sorted(path for path in paths if path)


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
        if matches_any(path, include) and not matches_any(path, exclude):
            semantic.append(path)
        else:
            other.append(path)
    return semantic, other


# ---------------------------------------------------------------------------
# Documentation Freshness
# ---------------------------------------------------------------------------


PATH_IN_CODE = re.compile(r"`([^`\n]+)`")
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
TREE_ROOT = re.compile(r"^([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/)$")
TREE_ITEM = re.compile(r"^[\s│]*[├└]──\s+([^\s#]+)")
GLOB_CHARS = "*?[]"


def doc_impact_path(feature):
    return os.path.join(feature, "doc-impact.json")


def garden_report_path(feature):
    return os.path.join(feature, "garden-report.json")


def clean_declared_path(text):
    """문서에 적힌 경로 표기에서 locator/장식만 걷어낸다.

    leading '.' 을 지우면 '.ai-team/' 이 'ai-team/' 이 되어 없는 경로처럼 보인다.
    그래서 왼쪽은 건드리지 않는다.
    """
    value = (text or "").strip()
    value = value.rstrip(",;")
    # 'path/to/file.c:128-129' 같은 line locator 를 떼어낸다.
    match = re.match(r"^(.+?):[0-9]+(?:-[0-9]+)?$", value)
    if match:
        value = match.group(1)
    return value.strip()


def looks_like_repo_path(text):
    """문서 안의 조각이 repository 경로 선언처럼 보이는지 본다.

    파일명만 적은 언급이나 'scope.include/exclude' 같은 JSON 필드 경로를
    경로 선언으로 오해하면 broken reference 신호가 잡음에 묻힌다.
    """
    text = (text or "").strip()
    if not text or " " in text:
        return False
    if any(ch in text for ch in GLOB_CHARS):
        return False
    if text.startswith("-") or text.startswith("+"):
        return False
    if "(" in text or ")" in text or "=" in text or "::" in text or "@" in text:
        return False
    # repository 경로 선언은 최소한 하나의 '/' 를 가진다.
    if "/" not in text:
        return False
    head = text.split("/", 1)[0]
    # '.ai-team' 처럼 dot 으로 시작하는 top-level 은 정상이다.
    # 반대로 'scope.include/exclude' 는 필드 경로지 파일 경로가 아니다.
    return not ("." in head and not head.startswith("."))


def extract_declared_paths(text):
    """문서 본문이 canonical 한 것처럼 선언하는 repository 경로를 뽑는다.

    세 형태만 본다 — inline code, markdown link, directory tree block.
    산문에서 경로를 추측하지 않는다.
    """
    results = []
    tree_root = None
    for line_no, line in enumerate(text.splitlines(), start=1):
        item = TREE_ITEM.match(line)
        if item:
            name = item.group(1)
            results.append(((tree_root + name) if tree_root else name, line_no))
            continue
        stripped = line.strip()
        if not stripped:
            tree_root = None
        elif TREE_ROOT.match(stripped):
            tree_root = stripped
            continue
        for match in PATH_IN_CODE.finditer(line):
            results.append((clean_declared_path(match.group(1)), line_no))
        for match in MD_LINK.finditer(line):
            results.append((clean_declared_path(match.group(1)), line_no))
    return results


def reference_candidates(root, doc_dir, value):
    """참조가 가리킬 수 있는 repository-relative 경로 후보를 만든다.

    '../pinesky-lib' 처럼 repository 밖을 가리키는 참조는 후보가 비어 있다.
    이 검사에서 '없는 경로'라고 단정할 근거가 없으므로 대상에서 뺀다.
    """
    raw = []
    if not value.startswith(".."):
        raw.append(value)
    if doc_dir:
        raw.append(os.path.normpath(os.path.join(doc_dir, value)))
    else:
        raw.append(os.path.normpath(value))
    inside = []
    for candidate in raw:
        candidate = norm_rel(candidate)
        if not candidate or candidate.startswith(".."):
            continue
        if candidate not in inside:
            inside.append(candidate)
    return inside


def reference_exists(root, doc_dir, value):
    for candidate in reference_candidates(root, doc_dir, value):
        if os.path.exists(os.path.join(root, candidate)):
            return True
    return False


def scan_broken_references(root, policy, documents=None):
    """문서가 존재하지 않는 repository 경로를 가리키는지 본다.

    문서가 실제와 어긋났음을 기계적으로 판정할 수 있는 가장 확실한 신호다.
    """
    scan = policy.get("reference_scan") or {}
    targets = documents if documents is not None else (scan.get("documents") or [])
    ignore = scan.get("ignore_patterns") or []
    known_absent = scan.get("known_absent") or []
    # historical decision log 는 과거 시점의 사실을 적은 기록이다. 그때 존재하던
    # 경로가 지금 없는 것은 정상이며, 고치면 오히려 이력을 덮어쓰게 된다.
    historical = {
        norm_rel(item.get("path") or "") for item in policy.get("historical_documents") or []
    }
    findings = []
    for doc in targets:
        doc = norm_rel(doc)
        if doc in historical:
            continue
        full = os.path.join(root, doc)
        if not os.path.isfile(full):
            continue
        with open(full, encoding="utf-8") as handle:
            text = handle.read()
        doc_dir = os.path.dirname(doc)
        seen = set()
        for value, line_no in extract_declared_paths(text):
            if not looks_like_repo_path(value):
                continue
            if matches_any(value, ignore) or matches_any(value, known_absent):
                continue
            candidates = reference_candidates(root, doc_dir, value)
            if not candidates:
                # repository 밖 참조 — 이 검사의 판정 대상이 아니다.
                continue
            if any(matches_any(candidate, known_absent) for candidate in candidates):
                continue
            if reference_exists(root, doc_dir, value):
                continue
            key = (doc, value)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                {
                    "document": doc,
                    "reference": value,
                    "line": line_no,
                    "reason": "선언된 경로가 repository 에 없다",
                }
            )
    return findings


def load_ontology_bindings(root):
    """cortex.yaml binding 을 최소 파싱한다. 새 YAML 의존을 만들지 않는다."""
    path = os.path.join(root, ONTOLOGY_BINDINGS)
    if not os.path.isfile(path):
        return []
    bindings = []
    current = None
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- id:"):
                current = {"id": stripped.split(":", 1)[1].strip()}
                bindings.append(current)
                continue
            if current is None or ":" not in stripped or stripped.startswith("-"):
                continue
            key, value = stripped.split(":", 1)
            current[key.strip()] = value.strip()
    return bindings


def docs_referencing(root, paths, limit=40):
    """canonical 문서가 changed path 를 직접 언급하는지 git grep 으로 찾는다."""
    hits = {}
    for path in paths[:limit]:
        if len(path) < 6:
            continue
        try:
            out = subprocess.check_output(
                ["git", "grep", "-l", "-I", "-F", path, "--", "*.md"],
                cwd=root,
                stderr=subprocess.STDOUT,
            ).decode("utf-8", "replace")
        except subprocess.CalledProcessError:
            continue
        for line in out.splitlines():
            doc = norm_rel(line)
            if not doc.endswith(".md") or doc == path:
                continue
            hits.setdefault(doc, [])
            if path not in hits[doc]:
                hits[doc].append(path)
    return hits


def document_category(policy, path):
    path = norm_rel(path)
    best = None
    for item in policy.get("canonical_roots") or []:
        prefix = norm_rel(item.get("path") or "")
        if not prefix:
            continue
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, item.get("category") or "other", item.get("kind") or "canonical")
    if best:
        return best[1], best[2]
    return "other", "canonical"


def discover_impacted_documents(root, policy, semantic_paths, contract=None):
    """deterministic evidence 만으로 impacted document 후보를 만든다.

    '관련 문서를 알아서 찾아라' 로 시작하지 않는다. 다섯 축에서 나온 후보에만
    의미 판단을 붙인다.
    """
    found = {}

    def add(doc, axis, reason, evidence=None):
        doc = norm_rel(doc or "")
        if not doc or not os.path.exists(os.path.join(root, doc)):
            return
        if doc in semantic_paths:
            return
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
            if any(ch in target for ch in GLOB_CHARS):
                continue
            add(
                target,
                "impact_rules",
                rule.get("reason") or rule.get("id"),
                [".ai-team/policy/documentation.json#{}".format(rule.get("id"))],
            )

    bindings = load_ontology_bindings(root)
    touched_entities = set()
    for binding in bindings:
        source = norm_rel(binding.get("source") or "")
        if not source:
            continue
        if any(
            path == source or path.startswith(source.rstrip("/") + "/") for path in semantic_paths
        ):
            touched_entities.add(binding.get("entity"))
    for binding in bindings:
        if binding.get("entity") not in touched_entities:
            continue
        target = norm_rel(binding.get("source") or "")
        if target.endswith(".md"):
            add(
                target,
                "ontology_binding",
                "entity {} binding".format(binding.get("entity")),
                ["docs/ontology/bindings/cortex.yaml#{}".format(binding.get("id"))],
            )

    try:
        knowledge = load_json(os.path.join(root, KNOWLEDGE_MAP))
    except Exception:
        knowledge = {}
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

    try:
        decisions = load_json(os.path.join(root, DECISIONS))
    except Exception:
        decisions = {}
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

    for doc, refs in docs_referencing(root, semantic_paths).items():
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
    feature = resolve_feature(root, feature_raw)
    contract = load_json(contract_path(feature))
    policy_file = os.path.join(root, DOCUMENTATION_POLICY)
    policy = load_json(policy_file)
    feature_rel = rel(root, feature)

    sources = list(changed) if changed else collect_changed_sources(root, work_base_commit(root))
    sources = [path for path in sources if not path.startswith(feature_rel.rstrip("/") + "/")]
    semantic, non_semantic = split_semantic_changes(sources, policy)

    # 사람이 '영향 없음'으로 판단한 문서는 기록으로 남기고 이어받는다.
    # 최상위 필드로 보존한다 — impacted_documents 는 매번 다시 계산되므로
    # 거기에만 두면 STALE 로 덮어쓸 때 판단 기록이 사라진다.
    acknowledged_paths = {norm_rel(path) for path in acknowledged or [] if path}
    target = doc_impact_path(feature)
    if os.path.isfile(target):
        try:
            previous = load_json(target)
            acknowledged_paths |= {
                norm_rel(path) for path in previous.get("acknowledged") or [] if path
            }
            for item in previous.get("impacted_documents") or []:
                if item.get("action") == "acknowledge":
                    acknowledged_paths.add(norm_rel(item.get("path") or ""))
        except Exception:
            pass

    # 이번 Work 가 실제로 손댄 문서는 이미 처리된 것으로 본다.
    touched = set(sources)
    documents = []
    if semantic:
        discovered = discover_impacted_documents(root, policy, semantic, contract)
        for path in sorted(discovered):
            if work_scoped_artifact(policy, path):
                continue
            item = discovered[path]
            category, kind = document_category(policy, path)
            if kind == "historical":
                # 과거 기록은 덮어쓰지 않는다. 새 결정이 생기면 supersede 로 잇는다.
                documents.append(
                    {
                        "path": path,
                        "category": category,
                        "reason": "; ".join(item["reasons"][:3]) or "impacted by change",
                        "action": "supersede",
                        "state": "ACTIVE",
                        "handled": True,
                        "discovered_by": item["axes"],
                        "evidence": item["evidence"][:6],
                    }
                )
                continue
            acknowledged_here = path in acknowledged_paths
            handled = path in touched or acknowledged_here
            documents.append(
                {
                    "path": path,
                    "category": category,
                    "reason": "; ".join(item["reasons"][:3]) or "impacted by change",
                    "action": "acknowledge"
                    if (acknowledged_here and path not in touched)
                    else "update",
                    "state": "ACTIVE" if handled else "STALE",
                    "handled": handled,
                    "discovered_by": item["axes"],
                    "evidence": item["evidence"][:6],
                }
            )

    broken = scan_broken_references(root, policy)
    unresolved = [item["path"] for item in documents if not item["handled"]]

    if not semantic:
        status = "NOT_APPLICABLE"
        reason = "의미 변경 경로가 없다"
    elif broken:
        status = "STALE"
        reason = "broken reference %d 건이 남아 있다" % len(broken)
    elif unresolved:
        status = "STALE"
        reason = "impacted document %d 건이 아직 처리되지 않았다: %s" % (
            len(unresolved),
            ", ".join(unresolved[:3]),
        )
    elif documents:
        status = "RESOLVED"
        reason = "impacted document %d 건을 이번 Work 에서 모두 갱신했다" % len(documents)
    else:
        status = "ACTIVE"
        reason = "의미 변경이 있으나 impacted document 후보가 없다"

    tree = worktree_state(root)
    value = {
        "schema_version": "1.0",
        "work_id": contract.get("id"),
        "status": status,
        "reason": reason,
        "semantic_change": bool(semantic),
        "changed_sources": semantic,
        "non_semantic_sources": non_semantic,
        "impacted_documents": documents,
        "acknowledged": sorted(acknowledged_paths),
        "broken_references": broken,
        "evidence": [axis.get("id") for axis in policy.get("discovery_axes") or []],
        "generated_from": {
            "commit": tree["commit"],
            "worktree_state": tree["state"],
            "worktree_dirty": tree["dirty"],
            "generated_at": utc_now(),
            "contract_sha256": file_sha(contract_path(feature)),
            "policy_sha256": file_sha(policy_file),
        },
    }
    value["content_hash"] = object_sha(value)
    if write:
        write_json(doc_impact_path(feature), value)
    return value


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
    errors = []
    broken = scan_broken_references(root, policy)
    history = validate_decision_history(root, policy)

    if repo:
        for item in broken:
            errors.append(
                "broken reference: {}:{} -> {}".format(
                    item["document"], item["line"], item["reference"]
                )
            )
        errors.extend(history["errors"])
        return {
            "valid": not errors,
            "verdict": "FRESH" if not errors else "STALE",
            "mode": "repository",
            "documents_scanned": len((policy.get("reference_scan") or {}).get("documents") or []),
            "broken_references": broken,
            "decision_history": history,
            "errors": errors,
        }

    feature = resolve_feature(root, feature_raw)
    path = doc_impact_path(feature)
    if not os.path.isfile(path):
        return {
            "valid": False,
            "verdict": "MISSING",
            "mode": "work",
            "errors": ["doc-impact.json 이 없다. docs impact 를 먼저 실행한다"],
            "path": rel(root, path),
        }
    value = load_json(path)
    required = [
        "schema_version",
        "work_id",
        "status",
        "changed_sources",
        "impacted_documents",
        "evidence",
        "generated_from",
        "content_hash",
    ]
    errors.extend(f"필수 필드 없음: {key}" for key in required if key not in value)
    if value.get("status") not in ("NOT_APPLICABLE", "ACTIVE", "STALE", "RESOLVED"):
        errors.append("freshness status 가 허용값이 아님: {}".format(value.get("status")))
    expected = object_sha(value)
    if value.get("content_hash") != expected:
        errors.append("content_hash 불일치")
    for item in value.get("impacted_documents") or []:
        if item.get("action") in (None, "pending"):
            errors.append(
                "impacted document 의 action 이 정해지지 않음: {}".format(item.get("path"))
            )
        if not item.get("discovered_by"):
            errors.append("impacted document 에 discovery 근거가 없음: {}".format(item.get("path")))
        target = norm_rel(item.get("path") or "")
        if target and not os.path.exists(os.path.join(root, target)):
            errors.append(f"impacted document 가 없음: {target}")
    if value.get("status") == "STALE":
        errors.append("doc freshness 가 STALE 이다. update/supersede 후 다시 impact 를 만든다")
    errors.extend(history["errors"])
    return {
        "valid": not errors,
        "verdict": value.get("status"),
        "mode": "work",
        "path": rel(root, path),
        "expected_hash": expected,
        "broken_references": value.get("broken_references") or [],
        "decision_history": history,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Repository Gardening
# ---------------------------------------------------------------------------


SAFE_TYPES = ("generated_garbage", "temporary_backup", "prunable_worktree")


def git_lines(root, args):
    return [norm_rel(line) for line in git(root, args).splitlines() if line.strip()]


def repo_file_sets(root):
    """tracked / untracked / ignored 를 나눠서 돌려준다.

    SAFE_AUTO 자동 삭제는 git 이 추적하지 않는 파일에만 허용한다. 되돌릴 수
    없는 삭제를 만들지 않기 위해서다.
    """
    tracked = set(git_lines(root, ["ls-files"]))
    untracked = set(git_lines(root, ["ls-files", "--others", "--exclude-standard"]))
    ignored = set(git_lines(root, ["ls-files", "--others", "--ignored", "--exclude-standard"]))
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
    action = {"SAFE_AUTO": "delete", "EVIDENCE_REQUIRED": "review", "HUMAN_GATED": "propose"}[
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
        return True
    for entry in scope:
        entry = norm_rel(entry)
        if not entry or entry == ".":
            return True
        if path == entry or path.startswith(entry.rstrip("/") + "/"):
            return True
    return False


def garden_scope(root, contract, mode):
    if mode == "full":
        return ["."]
    scope = [norm_rel(p) for p in (contract.get("scope") or {}).get("include") or []]
    scope.extend(collect_changed_sources(root))
    neighborhood = set()
    for path in scope:
        parent = os.path.dirname(path)
        if parent:
            neighborhood.add(parent)
    return sorted({p for p in scope if p} | neighborhood)


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
                        "git",
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
            except subprocess.CalledProcessError:
                continue
            except Exception:
                referenced = True
                break
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
    safe_patterns = policy.get("safe_auto_patterns") or []
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
    for item in prunable_worktrees(root):
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
    except Exception as exc:
        notes.append(f"documentation reference scan 생략: {exc}")

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
            "reference scan 상한 %d 개에 도달해 %d 개 파일을 검사하지 않았다"
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
        "pass": not integrity["violations"],
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


def garden_full(root, report_only=True, output=None):
    policy = load_json(os.path.join(root, GARDENING_POLICY))
    scope = ["."]
    candidates, notes = collect_garden_candidates(root, policy, scope, "full")
    applied = []
    if not report_only:
        applied = apply_safe_candidates(root, candidates)
    value = build_garden_report(root, policy, "full", scope, candidates, applied, notes, None)
    if output:
        write_json(safe_path(root, output), value)
    return value


def apply_safe_candidates(root, candidates):
    """SAFE_AUTO candidate 만 삭제한다.

    EVIDENCE_REQUIRED 와 HUMAN_GATED 는 어떤 경우에도 여기서 지우지 않는다.
    """
    applied = []
    for item in candidates:
        if item.get("safety") != "SAFE_AUTO":
            item["status"] = "gated" if item.get("safety") == "HUMAN_GATED" else "kept"
            continue
        path = norm_rel((item.get("target") or {}).get("path") or "")
        if not path:
            continue
        try:
            full = safe_path(root, path)
        except RuntimeError:
            item["status"] = "kept"
            continue
        if item.get("type") == "prunable_worktree":
            git(root, ["worktree", "prune"])
            item["status"] = "applied"
            applied.append(path)
            continue
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
        elif os.path.exists(full):
            os.remove(full)
        else:
            item["status"] = "kept"
            continue
        item["status"] = "applied"
        applied.append(path)
    return applied


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
    active_terms = {}
    try:
        script_dir = os.path.join(root, "tools", "ontology")
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        from semantic_runtime import SemanticRuntime, local_name

        runtime = SemanticRuntime(root=root)

        def namespace_of(uri):
            text = str(uri)
            for sep in ("#", "/", ":"):
                idx = text.rfind(sep)
                if idx > 0:
                    return text[:idx]
            return text

        def types_of(subject):
            try:
                from rdflib import RDF

                return sorted(str(o) for o in runtime.graph.objects(subject, RDF.type))
            except Exception:
                return []

        for subject in set(runtime.graph.subjects()):
            name = local_name(subject).lower()
            previous = active_terms.get(name)
            if previous is not None and str(subject) != previous["uri"]:
                # local name 이 같아도 namespace 가 다르면 RDF 에서는 정상 구분이다.
                # 실제 위험은 "같은 종류의 것을 두 번 선언"한 경우다 — 그때만 warn.
                current_types = types_of(subject)
                shared_type = bool(set(current_types) & set(previous["types"]))
                same_namespace = namespace_of(subject) == namespace_of(previous["uri"])
                if shared_type or same_namespace:
                    findings.append(
                        {
                            "type": "ontology_local_name_collision",
                            "name": name,
                            "uris": [previous["uri"], str(subject)],
                            "rdf_types": [previous["types"], current_types],
                            "severity": "warn",
                            "reason": "동일 namespace"
                            if same_namespace
                            else "동일 rdf:type 으로 중복 선언 의심",
                        }
                    )
                else:
                    findings.append(
                        {
                            "type": "ontology_local_name_shared",
                            "name": name,
                            "uris": [previous["uri"], str(subject)],
                            "rdf_types": [previous["types"], current_types],
                            "severity": "info",
                            "reason": "namespace 와 rdf:type 이 모두 달라 의미 모호성 없음",
                        }
                    )
            active_terms[name] = {"uri": str(subject), "types": types_of(subject)}
    except Exception as exc:
        findings.append(
            {"type": "semantic_runtime_unavailable", "error": str(exc), "severity": "block"}
        )
    return {
        "pass": not any(item.get("severity") == "block" for item in findings),
        "finding_count": len(findings),
        "findings": findings,
        "rule": "proposal only; harness/ontology를 자동 수정하지 않는다",
    }

#!/usr/bin/env python3
"""Run AMPLAI verifier profiles from .ai-team/verifiers/registry.json.

Exit codes:
  0  all blocking checks passed (warnings may have failed)
  1  one or more blocking checks failed
  2  invalid registry/arguments or requested blocking check unavailable

stdlib only. 이 runner 자체는 어느 interpreter 로도 돌지만, 등록된 check 는
pyproject 의 requires-python 을 만족하는 interpreter 를 부른다. report 는 그것을
registry command 에서 관찰해 기록한다.
"""
import argparse
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import time


DEFAULT_REGISTRY = os.path.join(".ai-team", "verifiers", "registry.json")



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


def verifier_interpreters(registry):
    """check command 가 실제로 부르는 python interpreter 를 뽑는다.

    상수로 적어 두면 registry 가 바뀌어도 report 는 옛 주장을 계속 한다.
    command 를 해석하지 못하면 빈 목록이 아니라 그 사실을 돌려준다.
    """
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
            unparsed.append(item.get("id") or command)
    values = sorted(found)
    if unparsed:
        values.append(
            "unobserved: command에서 interpreter를 못 읽음 (%s)" % ", ".join(sorted(unparsed))
        )
    return values


def repo_root():
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.STDOUT
        )
        return out.decode("utf-8", "replace").strip()
    except Exception:
        return os.getcwd()


def load_json(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def first_line(command):
    try:
        out = subprocess.check_output(command, stderr=subprocess.STDOUT)
        lines = out.decode("utf-8", "replace").strip().splitlines()
        return lines[0] if lines else ""
    except Exception as exc:
        return "unavailable: %s" % exc


def sha256_file(path):
    digest = hashlib.sha256()
    try:
        with io.open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return ""


def environment_fingerprint(root, registry_path, registry=None):
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode("utf-8", "replace").strip()
    except Exception:
        commit = "WORKTREE"
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "git": first_line(["git", "--version"]),
        "commit": commit,
        "registry_sha256": sha256_file(registry_path),
        "target_assumptions": {
            "verified_on": platform.platform(),
            "verifier_interpreter": verifier_interpreters(registry or {}),
            "not_verified": [
                "다른 OS·architecture 에서의 동작",
                "pyproject 의 requires-python 아래 버전에서의 동작"
            ],
            "python_requirement": python_requirement(root)
        }
    }


def host_platform():
    value = sys.platform.lower()
    if value.startswith("linux"):
        return "linux"
    if value.startswith("darwin"):
        return "darwin"
    if value.startswith("win") or value.startswith("cygwin"):
        return "windows"
    return platform.system().lower() or value


def resolve_profile(name, profiles, stack=None):
    stack = list(stack or [])
    if name not in profiles:
        raise ValueError("unknown verifier profile: %s" % name)
    if name in stack:
        raise ValueError("profile extension cycle: %s" % " -> ".join(stack + [name]))
    stack.append(name)
    ordered = []
    for parent in profiles[name].get("extends") or []:
        ordered.extend(resolve_profile(parent, profiles, stack))
    ordered.extend(profiles[name].get("checks") or [])
    seen = set()
    result = []
    for check_id in ordered:
        if check_id not in seen:
            seen.add(check_id)
            result.append(check_id)
    return result


def indent(text):
    if not text:
        return ""
    return "\n".join("    " + line for line in text.rstrip("\n").splitlines())


def write_report(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    fd, tmp = tempfile.mkstemp(prefix=".verifier-", suffix=".json", dir=directory)
    try:
        with io.open(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.rename(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--check", action="append", default=[])
    parser.add_argument("--list", action="store_true", dest="list_only")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-report")
    parser.add_argument("--commit-message-file")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    root = repo_root()
    os.chdir(root)
    registry_path = args.registry
    if not os.path.isabs(registry_path):
        registry_path = os.path.join(root, registry_path)

    try:
        registry = load_json(registry_path)
    except Exception as exc:
        print("verifier: cannot read %s: %s" % (registry_path, exc), file=sys.stderr)
        return 2

    profiles = registry.get("profiles") or {}
    check_defs = registry.get("checks") or []
    checks = {}
    for item in check_defs:
        check_id = item.get("id")
        if not check_id:
            print("verifier: check without id", file=sys.stderr)
            return 2
        if check_id in checks:
            print("verifier: duplicate check id: %s" % check_id, file=sys.stderr)
            return 2
        checks[check_id] = item

    if args.list_only:
        print("profiles:")
        for name in sorted(profiles):
            try:
                ids = resolve_profile(name, profiles)
            except ValueError as exc:
                print("  %s: INVALID (%s)" % (name, exc))
                continue
            desc = profiles[name].get("description") or ""
            print("  %-10s %s" % (name, desc))
            print("             %s" % ", ".join(ids))
        print("checks:")
        for check_id in sorted(checks):
            item = checks[check_id]
            print("  %-28s %-5s %s" % (
                check_id, item.get("severity", "block"), item.get("command", "")
            ))
        return 0

    selected = []
    try:
        if args.profile:
            selected.extend(resolve_profile(args.profile, profiles))
        selected.extend(args.check)
    except ValueError as exc:
        print("verifier: %s" % exc, file=sys.stderr)
        return 2

    if not selected:
        print("verifier: choose --profile or --check", file=sys.stderr)
        return 2

    deduped = []
    seen = set()
    for check_id in selected:
        if check_id not in checks:
            print("verifier: unknown check: %s" % check_id, file=sys.stderr)
            return 2
        if check_id not in seen:
            seen.add(check_id)
            deduped.append(check_id)
    selected = deduped

    commit_message = None
    if args.commit_message_file:
        try:
            with io.open(args.commit_message_file, "r", encoding="utf-8") as handle:
                commit_message = handle.read()
        except Exception as exc:
            print("verifier: cannot read commit message: %s" % exc, file=sys.stderr)
            return 2
    elif "COMMIT_MSG" in os.environ:
        commit_message = os.environ.get("COMMIT_MSG", "")

    print("==== AMPLAI verifier ====")
    print("repo    : %s" % root)
    if args.profile:
        print("profile : %s" % args.profile)
    print("checks  : %s" % ", ".join(selected))
    print("platform: %s" % host_platform())
    if args.dry_run:
        print("mode    : dry-run")
    print("")

    results = []
    blocking_failed = False
    blocking_unavailable = False
    host = host_platform()

    for check_id in selected:
        item = checks[check_id]
        severity = item.get("severity", "block")
        command = item.get("command") or ""
        started = time.time()
        status = "PASS"
        rc = 0
        output = ""
        reason = ""

        print("--- %s [%s]" % (check_id, severity))

        allowed = item.get("platforms") or []
        if not command:
            status = "UNAVAILABLE"
            rc = 2
            reason = "empty command"
        elif args.dry_run:
            status = "PLANNED"
            reason = command
        elif allowed and host not in allowed:
            status = "UNAVAILABLE"
            rc = 2
            reason = "platform %s not in %s" % (host, ",".join(allowed))
        elif item.get("context") == "commit-message" and commit_message is None:
            status = "UNAVAILABLE"
            rc = 2
            reason = "pending commit message is required"
        else:
            env = os.environ.copy()
            if commit_message is not None:
                env["COMMIT_MSG"] = commit_message
            proc = subprocess.Popen(
                command,
                cwd=root,
                env=env,
                shell=True,
                executable="/bin/bash",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            output, _ = proc.communicate()
            rc = int(proc.returncode)
            if rc != 0:
                status = "WARN" if severity == "warn" else "FAIL"

        duration = round(time.time() - started, 3)
        if output:
            print(indent(output))
        if reason:
            print("    %s" % reason)
        print("    %s (exit %s, %.3fs)" % (status, rc, duration))

        if status == "FAIL" and severity == "block":
            blocking_failed = True
        if status == "UNAVAILABLE" and severity == "block":
            blocking_unavailable = True

        results.append({
            "id": check_id,
            "severity": severity,
            "status": status,
            "exit_code": rc,
            "duration_seconds": duration,
            "command": command,
            "source": item.get("source"),
            "reason": reason,
            "output": output,
        })

    if args.dry_run:
        verdict = "PLANNED"
        exit_code = 0
    elif blocking_unavailable:
        verdict = "UNAVAILABLE"
        exit_code = 2
    elif blocking_failed:
        verdict = "FAIL"
        exit_code = 1
    else:
        verdict = "PASS"
        exit_code = 0

    payload = {
        "schema_version": "2.0",
        "project": registry.get("project"),
        "profile": args.profile,
        "verdict": verdict,
        "exit_code": exit_code,
        "platform": host,
        "environment": environment_fingerprint(root, registry_path, registry),
        "results": results,
    }
    if args.json_report:
        try:
            write_report(args.json_report, payload)
        except Exception as exc:
            print("verifier: cannot write report: %s" % exc, file=sys.stderr)
            return 2

    print("")
    print("VERIFIER: %s" % verdict)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

"""Egress profile qualification gate (EGR-01, design 10_AUTHORITY_SECURITY).

Pure unit tests: EgressProfile validation/digest/env, qualification_ref assembly,
require_qualified/load_qualification gating, and ContainerSandbox integration
(argv proxy env, network gating, ENV_AUTHORITY rejection of proxy names).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.sandbox.container import ContainerProfile, ContainerSandbox
from amplai_foundry.sandbox.egress import (
    REQUIRED_PROBES,
    EgressProfile,
    load_qualification,
    qualification_ref,
    require_qualified,
)

IMAGE = "registry/worker@sha256:" + "a" * 64


def _profile(**overrides: object) -> EgressProfile:
    kwargs: dict[str, object] = {
        "name": "prof-a",
        "network": "amplai-internal",
        "proxy": "proxy.internal:3128",
        "allow": ("api.example.com:443", "b.example.com:443"),
    }
    kwargs.update(overrides)
    return EgressProfile(**kwargs)  # type: ignore[arg-type]


def _good_probes() -> dict[str, dict[str, object]]:
    return {
        name: {"outcome": "pass", "observation": f"obs-{name}", "command": f"cmd-{name}"}
        for name in REQUIRED_PROBES
    }


def _good_ref(profile: EgressProfile) -> dict[str, object]:
    return qualification_ref(
        profile, _good_probes(), checked_at="2026-09-20T00:00:00Z", evidence="ev"
    )


# --- EgressProfile validation ---


def test_egress_profile_rejects_invalid_name() -> None:
    with pytest.raises(RuntimeFault) as exc:
        _profile(name="bad name!")
    assert exc.value.code == "EGRESS_PROFILE"


def test_egress_profile_rejects_invalid_network() -> None:
    with pytest.raises(RuntimeFault) as exc:
        _profile(network="bad network!")
    assert exc.value.code == "EGRESS_PROFILE"


def test_egress_profile_rejects_proxy_without_port() -> None:
    with pytest.raises(RuntimeFault) as exc:
        _profile(proxy="proxy.internal")
    assert exc.value.code == "EGRESS_PROXY"


def test_egress_profile_rejects_empty_allowlist() -> None:
    with pytest.raises(RuntimeFault) as exc:
        _profile(allow=())
    assert exc.value.code == "EGRESS_ALLOWLIST"


def test_egress_profile_rejects_wildcard_allowlist_entry() -> None:
    with pytest.raises(RuntimeFault) as exc:
        _profile(allow=("*.example.com:443",))
    assert exc.value.code == "EGRESS_ALLOWLIST"


def test_egress_profile_rejects_allowlist_entry_missing_port() -> None:
    with pytest.raises(RuntimeFault) as exc:
        _profile(allow=("api.example.com",))
    assert exc.value.code == "EGRESS_ALLOWLIST"


def test_egress_profile_rejects_duplicate_allowlist_entry() -> None:
    with pytest.raises(RuntimeFault) as exc:
        _profile(allow=("api.example.com:443", "api.example.com:443"))
    assert exc.value.code == "EGRESS_ALLOWLIST"


def test_egress_profile_allow_is_sorted_tuple() -> None:
    profile = _profile(allow=("b.example.com:443", "api.example.com:443"))
    assert profile.allow == ("api.example.com:443", "b.example.com:443")
    assert isinstance(profile.allow, tuple)


def test_egress_profile_digest_stable_and_changes_with_allowlist() -> None:
    profile = _profile()
    same = _profile()
    assert profile.digest() == same.digest()
    different = _profile(allow=("api.example.com:443", "c.example.com:443"))
    assert profile.digest() != different.digest()


def test_egress_profile_env_returns_proxy_vars() -> None:
    profile = _profile()
    assert profile.env() == {
        "HTTPS_PROXY": "http://proxy.internal:3128",
        "HTTP_PROXY": "http://proxy.internal:3128",
        "NO_PROXY": "localhost,127.0.0.1",
    }


def test_egress_profile_wire_includes_digest() -> None:
    profile = _profile()
    wire = profile.wire()
    assert wire["digest"] == profile.digest()
    assert wire["name"] == profile.name
    assert wire["allow"] == list(profile.allow)


def test_egress_profile_load_round_trips(tmp_path: Path) -> None:
    profile = _profile()
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile.wire()))
    loaded = EgressProfile.load(path)
    assert loaded.digest() == profile.digest()


def test_egress_profile_load_missing_file_raises_hold(tmp_path: Path) -> None:
    with pytest.raises(Hold) as exc:
        EgressProfile.load(tmp_path / "missing.json")
    assert exc.value.code == "EGRESS_PROFILE_UNREADABLE"


def test_egress_profile_load_malformed_json_raises_hold(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(Hold) as exc:
        EgressProfile.load(path)
    assert exc.value.code == "EGRESS_PROFILE_UNREADABLE"


def test_egress_profile_load_missing_fields_raises_runtime_fault(tmp_path: Path) -> None:
    path = tmp_path / "partial.json"
    path.write_text(json.dumps({"name": "x", "network": "y"}))
    with pytest.raises(RuntimeFault) as exc:
        EgressProfile.load(path)
    assert exc.value.code == "EGRESS_PROFILE"


# --- qualification_ref ---


def test_qualification_ref_all_probes_pass_yields_pass_outcome() -> None:
    profile = _profile()
    ref = qualification_ref(profile, _good_probes(), checked_at="t", evidence="e")
    assert ref["outcome"] == "pass"
    assert [c["name"] for c in ref["checks"]] == list(REQUIRED_PROBES)
    assert all(c["outcome"] == "pass" for c in ref["checks"])


def test_qualification_ref_missing_probe_is_inconclusive_and_fails() -> None:
    profile = _profile()
    probes = _good_probes()
    del probes["denied_target_refused"]
    ref = qualification_ref(profile, probes, checked_at="t", evidence="e")
    assert ref["outcome"] == "fail"
    missing = next(c for c in ref["checks"] if c["name"] == "denied_target_refused")
    assert missing["outcome"] == "inconclusive"


def test_qualification_ref_probe_without_observation_is_inconclusive() -> None:
    profile = _profile()
    probes = _good_probes()
    probes["allowed_target_tunnels"] = {"outcome": "pass", "observation": ""}
    ref = qualification_ref(profile, probes, checked_at="t", evidence="e")
    check = next(c for c in ref["checks"] if c["name"] == "allowed_target_tunnels")
    assert check["outcome"] == "inconclusive"
    assert ref["outcome"] == "fail"


def test_qualification_ref_probe_outcome_fail_fails_ref() -> None:
    profile = _profile()
    probes = _good_probes()
    probes["direct_egress_denied"] = {"outcome": "fail", "observation": "leaked"}
    ref = qualification_ref(profile, probes, checked_at="t", evidence="e")
    assert ref["outcome"] == "fail"
    check = next(c for c in ref["checks"] if c["name"] == "direct_egress_denied")
    assert check["outcome"] == "fail"


# --- require_qualified ---


def test_require_qualified_none_ref_raises_hold() -> None:
    profile = _profile()
    with pytest.raises(Hold) as exc:
        require_qualified(profile, None)
    assert exc.value.code == "EGRESS_UNQUALIFIED"


def test_require_qualified_fail_outcome_raises_hold() -> None:
    profile = _profile()
    ref = _good_ref(profile)
    ref["outcome"] = "fail"
    with pytest.raises(Hold) as exc:
        require_qualified(profile, ref)
    assert exc.value.code == "EGRESS_UNQUALIFIED"


def test_require_qualified_digest_mismatch_raises_hold() -> None:
    profile = _profile()
    other = _profile(allow=("api.example.com:443", "c.example.com:443"))
    ref = _good_ref(other)
    with pytest.raises(Hold) as exc:
        require_qualified(profile, ref)
    assert exc.value.code == "EGRESS_UNQUALIFIED"


def test_require_qualified_missing_probe_in_checks_raises_hold() -> None:
    profile = _profile()
    ref = _good_ref(profile)
    checks = cast("list[dict[str, object]]", ref["checks"])
    ref["checks"] = [c for c in checks if c["name"] != "allowed_target_tunnels"]
    with pytest.raises(Hold) as exc:
        require_qualified(profile, ref)
    assert exc.value.code == "EGRESS_UNQUALIFIED"


def test_require_qualified_good_ref_passes_silently() -> None:
    profile = _profile()
    require_qualified(profile, _good_ref(profile))


# --- load_qualification ---


def test_load_qualification_missing_file_raises_hold(tmp_path: Path) -> None:
    with pytest.raises(Hold) as exc:
        load_qualification(tmp_path / "missing.json")
    assert exc.value.code == "EGRESS_UNQUALIFIED"


def test_load_qualification_non_object_json_raises_hold(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(Hold) as exc:
        load_qualification(path)
    assert exc.value.code == "EGRESS_UNQUALIFIED"


# --- ContainerSandbox integration ---


def test_container_sandbox_network_none_without_egress_has_no_proxy_argv(tmp_path: Path) -> None:
    box = ContainerSandbox(ContainerProfile(IMAGE))
    argv = box.command(["echo", "hi"], workspace=tmp_path, run_name="x")
    assert "HTTPS_PROXY" not in " ".join(argv)


def test_container_sandbox_network_none_with_egress_raises_runtime_fault() -> None:
    profile = _profile()
    with pytest.raises(RuntimeFault) as exc:
        ContainerSandbox(ContainerProfile(IMAGE, network="none", egress=profile))
    assert exc.value.code == "EGRESS_PROFILE"


def test_container_sandbox_network_set_without_egress_raises_hold() -> None:
    with pytest.raises(Hold) as exc:
        ContainerSandbox(ContainerProfile(IMAGE, network="amplai-internal"))
    assert exc.value.code == "EGRESS_UNQUALIFIED"


def test_container_sandbox_network_name_mismatch_raises_hold() -> None:
    profile = _profile(network="amplai-internal")
    with pytest.raises(Hold) as exc:
        ContainerSandbox(ContainerProfile(IMAGE, network="other-network", egress=profile))
    assert exc.value.code == "EGRESS_UNQUALIFIED"


def test_container_sandbox_network_matches_but_ref_none_raises_hold() -> None:
    profile = _profile(network="amplai-internal")
    with pytest.raises(Hold) as exc:
        ContainerSandbox(ContainerProfile(IMAGE, network="amplai-internal", egress=profile))
    assert exc.value.code == "EGRESS_UNQUALIFIED"


def test_container_sandbox_network_matches_but_ref_fail_raises_hold() -> None:
    profile = _profile(network="amplai-internal")
    ref = _good_ref(profile)
    ref["outcome"] = "fail"
    with pytest.raises(Hold) as exc:
        ContainerSandbox(
            ContainerProfile(
                IMAGE, network="amplai-internal", egress=profile, network_qualification_ref=ref
            )
        )
    assert exc.value.code == "EGRESS_UNQUALIFIED"


def test_container_sandbox_network_matches_but_wrong_digest_raises_hold() -> None:
    profile = _profile(network="amplai-internal")
    other = _profile(network="amplai-internal", allow=("api.example.com:443", "c.example.com:443"))
    ref = _good_ref(other)
    with pytest.raises(Hold) as exc:
        ContainerSandbox(
            ContainerProfile(
                IMAGE, network="amplai-internal", egress=profile, network_qualification_ref=ref
            )
        )
    assert exc.value.code == "EGRESS_UNQUALIFIED"


def test_container_sandbox_good_ref_builds_and_argv_has_proxy_env(tmp_path: Path) -> None:
    profile = _profile(network="amplai-internal")
    ref = _good_ref(profile)
    box = ContainerSandbox(
        ContainerProfile(
            IMAGE, network="amplai-internal", egress=profile, network_qualification_ref=ref
        )
    )
    argv = box.command(["echo", "hi"], workspace=tmp_path, run_name="x")
    assert "--network" in argv and argv[argv.index("--network") + 1] == "amplai-internal"
    assert "--env" in argv and "HTTPS_PROXY=http://proxy.internal:3128" in argv
    assert "HTTP_PROXY=http://proxy.internal:3128" in argv
    assert "NO_PROXY=localhost,127.0.0.1" in argv
    assert "--init" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv


def test_container_sandbox_rejects_https_proxy_env_name(tmp_path: Path) -> None:
    profile = _profile(network="amplai-internal")
    ref = _good_ref(profile)
    box = ContainerSandbox(
        ContainerProfile(
            IMAGE, network="amplai-internal", egress=profile, network_qualification_ref=ref
        )
    )
    with pytest.raises(Hold) as exc:
        box.command(["echo"], workspace=tmp_path, run_name="x", env_names=["HTTPS_PROXY"])
    assert exc.value.code == "ENV_AUTHORITY"


def test_container_sandbox_rejects_http_proxy_env_name(tmp_path: Path) -> None:
    profile = _profile(network="amplai-internal")
    ref = _good_ref(profile)
    box = ContainerSandbox(
        ContainerProfile(
            IMAGE, network="amplai-internal", egress=profile, network_qualification_ref=ref
        )
    )
    with pytest.raises(Hold) as exc:
        box.command(["echo"], workspace=tmp_path, run_name="x", env_names=["HTTP_PROXY"])
    assert exc.value.code == "ENV_AUTHORITY"


def test_container_sandbox_rejects_no_proxy_env_name(tmp_path: Path) -> None:
    profile = _profile(network="amplai-internal")
    ref = _good_ref(profile)
    box = ContainerSandbox(
        ContainerProfile(
            IMAGE, network="amplai-internal", egress=profile, network_qualification_ref=ref
        )
    )
    with pytest.raises(Hold) as exc:
        box.command(["echo"], workspace=tmp_path, run_name="x", env_names=["NO_PROXY"])
    assert exc.value.code == "ENV_AUTHORITY"


def test_container_sandbox_allows_other_env_name_without_value(tmp_path: Path) -> None:
    box = ContainerSandbox(ContainerProfile(IMAGE))
    argv = box.command(
        ["echo"], workspace=tmp_path, run_name="x", env_names=["CLAUDE_CODE_OAUTH_TOKEN"]
    )
    assert "--env" in argv
    assert "CLAUDE_CODE_OAUTH_TOKEN" in argv
    assert not any(a.startswith("CLAUDE_CODE_OAUTH_TOKEN=") for a in argv)


def test_r002_require_qualified_rejects_pass_checks_without_observation():
    from amplai_foundry.sandbox.egress import (
        REQUIRED_PROBES,
        EgressProfile,
        qualification_ref,
        require_qualified,
    )

    profile = EgressProfile("p", "net", "proxy:3128", ("api.anthropic.com:443",))
    probes = {
        n: {"outcome": "pass", "observation": "measured", "command": "c"} for n in REQUIRED_PROBES
    }
    ref = qualification_ref(profile, probes, checked_at="t", evidence="e")
    require_qualified(profile, ref)
    forged = dict(ref, checks=[{"name": n, "outcome": "pass"} for n in REQUIRED_PROBES])
    with pytest.raises(Hold) as exc:
        require_qualified(profile, forged)
    assert exc.value.code == "EGRESS_UNQUALIFIED"
    duplicated = dict(ref, checks=[*ref["checks"][:2], ref["checks"][1]])
    with pytest.raises(Hold):
        require_qualified(profile, duplicated)

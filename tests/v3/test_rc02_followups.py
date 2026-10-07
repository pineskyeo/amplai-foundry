"""Review round 2 follow-ups R106 R401 (failure lens, specs/013 trace round 2).

R102 (cutover), R107 (V2 import) and R108/R402/R501 (Hermes identities) were removed with the
code they covered (specs/033-harness-taxonomy/legacy-inventory.md X10, X11, D2).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from amplai_foundry.runtime.errors import Hold


def test_r106_recover_removes_mkstemp_residue_and_reports_it(deployment, tmp_path):
    from amplai_foundry.distribution.installer import KitInstaller
    from amplai_foundry.distribution.packs import PackRegistry

    d = deployment
    installer = KitInstaller(PackRegistry(d.store, d.contracts, {}))
    root = tmp_path / "app"
    meta = root / ".ai-team" / "install-v3"
    meta.mkdir(parents=True)
    (meta / ".v3-abc123").write_bytes(b"half written")
    actor = replace(d.actor, permissions=d.actor.permissions | {"pack.install"})
    result = installer.recover(actor, root)
    assert result["status"] == "clean"
    assert result["temp_removed"] == [".ai-team/install-v3/.v3-abc123"]
    assert not (meta / ".v3-abc123").exists()


def test_r401_recover_takes_owner_lock_before_removing_residue(deployment, tmp_path):
    import fcntl

    from amplai_foundry.distribution.installer import KitInstaller
    from amplai_foundry.distribution.packs import PackRegistry

    d = deployment
    installer = KitInstaller(PackRegistry(d.store, d.contracts, {}))
    root = tmp_path / "app"
    meta = root / ".ai-team" / "install-v3"
    meta.mkdir(parents=True)
    temp = meta / ".v3-inflight"
    temp.write_bytes(b"about to be os.replace()d by a running apply()")
    actor = replace(d.actor, permissions=d.actor.permissions | {"pack.install"})
    with open(meta / "owner.lock", "a+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # simulate the running apply()
        with pytest.raises(Hold) as exc:
            installer.recover(actor, root)
        assert exc.value.code == "INSTALL_BUSY"
        assert temp.exists()  # nothing was removed while another owner held the lock
    result = installer.recover(actor, root)
    assert result["temp_removed"] == [".ai-team/install-v3/.v3-inflight"]
    assert not temp.exists()

"""Shared fixtures for end-to-end suites; the same isolated reference deployment as tests/v3."""

from __future__ import annotations

import pytest

from amplai_foundry.runtime.reference import ReferenceDeployment


@pytest.fixture
def deployment(tmp_path):
    with ReferenceDeployment(tmp_path / "deployment") as value:
        yield value

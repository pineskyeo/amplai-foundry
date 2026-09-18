from __future__ import annotations
import pytest
from amplai_foundry.runtime.reference import ReferenceDeployment
@pytest.fixture
def deployment(tmp_path):
    with ReferenceDeployment(tmp_path/'deployment') as value:
        yield value
@pytest.fixture
def prepared(deployment):
    return deployment.prepare(two_apps=True)

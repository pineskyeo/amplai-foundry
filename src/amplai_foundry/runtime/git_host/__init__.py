"""The ``git_host`` module kind (Work 034 OD-15, D-126): the code-host connection.

Only the interface is exported here. An implementation (``git_host/github.py``) is imported by
the composition root that selects it from configuration (``runtime/local_deployment.py``), so
the core publisher and outcome tracker never name a product.
"""

from __future__ import annotations

from .interface import KIND, PR_STATES, SUITE, GitHost, PullRequestState

__all__ = ["KIND", "PR_STATES", "SUITE", "GitHost", "PullRequestState"]

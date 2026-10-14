"""The ``service_manager`` module kind (Work 034 S3; spec C-8, U-2; D-118).

Only the interface is exported here. An implementation (``service_manager/launchd.py``,
``service_manager/systemd.py``) is imported by the composition root that selects it
(``runtime/local_deployment.py`` ``service_manager``), so no other code names a host product.
"""

from __future__ import annotations

from .interface import (
    ACTIONS,
    KIND,
    META_TICK,
    SERVER,
    SUITE,
    HostContext,
    RenderedFile,
    ServiceManager,
    ServiceUnit,
    amplai_units,
    freeze_sequence,
    unfreeze_sequence,
)

__all__ = [
    "ACTIONS",
    "KIND",
    "META_TICK",
    "SERVER",
    "SUITE",
    "HostContext",
    "RenderedFile",
    "ServiceManager",
    "ServiceUnit",
    "amplai_units",
    "freeze_sequence",
    "unfreeze_sequence",
]

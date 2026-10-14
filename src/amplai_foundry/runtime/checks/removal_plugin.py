"""The pytest plugin of the removal test (Work 034 M-13): a test run without one module.

``check.module_removal`` runs ``pytest -p amplai_foundry.runtime.checks.removal_plugin`` with:

- ``AMPLAI_CHECK_REMOVED_MODULE``: the module id (for the report);
- ``AMPLAI_CHECK_REMOVED_FILES``: comma-separated Python modules that become unimportable
  (``ModuleNotFoundError``), as if the module's files were not installed;
- ``AMPLAI_CHECK_OFF_KEY``: the ``local.json`` key whose default becomes ``null`` (the module
  turned off in every configuration that does not name it).

The finder is installed when pytest imports this plugin, before any conftest or test module, so
no product code has loaded the module yet. Without the variables the plugin does nothing.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType

ENV_MODULE = "AMPLAI_CHECK_REMOVED_MODULE"
ENV_FILES = "AMPLAI_CHECK_REMOVED_FILES"
ENV_OFF_KEY = "AMPLAI_CHECK_OFF_KEY"


class Removed(MetaPathFinder):
    """Refuses to find the removed modules (and anything under them)."""

    def __init__(self, names: Sequence[str]) -> None:
        self.names = tuple(names)

    def hides(self, fullname: str) -> bool:
        return any(fullname == n or fullname.startswith(n + ".") for n in self.names)

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if self.hides(fullname):
            raise ModuleNotFoundError(f"{fullname} is removed (removal test, M-13)", name=fullname)
        return None


def _install() -> None:
    files = [f for f in os.environ.get(ENV_FILES, "").split(",") if f]
    if not files:
        return
    finder = Removed(files)
    loaded = sorted(n for n in sys.modules if finder.hides(n))
    if loaded:  # too late to hide: the run would not test a removal
        raise RuntimeError(f"removed modules were already imported: {loaded}")
    sys.meta_path.insert(0, finder)
    key = os.environ.get(ENV_OFF_KEY)
    if not key:
        return
    try:
        from amplai_foundry.runtime.local_deployment import LocalConfig
    except ImportError:
        return  # the composition root needs the module: the tests that use it fail and say so
    field = LocalConfig.model_fields[key]
    field.default, field.default_factory = None, None
    LocalConfig.model_rebuild(force=True)


_install()

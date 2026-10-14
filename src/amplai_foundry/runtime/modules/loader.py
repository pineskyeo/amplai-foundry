"""Load built-in module code by its registry reference (Work 034 M-3, M-16).

A reference is ``package.module:attribute`` as :data:`.builtin.BUILTIN_CODE` and
:data:`.builtin.KIND_SUITES` write it. Only this package's own code loads: a reference outside
``amplai_foundry.`` is refused, and nothing scans installed entry points (M-3, AC-M3). Extension
modules come through :func:`.pinned.verify_extensions` instead.
"""

from __future__ import annotations

import importlib
import re

from ..errors import RuntimeFault

_REF = re.compile(r"(amplai_foundry(?:\.[a-z_][a-z0-9_]*)+):([A-Za-z_][A-Za-z0-9_]*)")


def load(ref: str) -> object:
    """The attribute ``ref`` names. ``MODULE_ENTRY`` when the reference is malformed, outside
    this package, or names nothing."""
    match = _REF.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        raise RuntimeFault("MODULE_ENTRY", "a built-in reference is amplai_foundry.<module>:<name>",
                           details={"ref": ref})  # fmt: skip
    module_name, attribute = match.groups()
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeFault(
            "MODULE_ENTRY", "the module of a built-in reference cannot be imported",
            details={"ref": ref, "error": type(exc).__name__},
        ) from exc  # fmt: skip
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise RuntimeFault(
            "MODULE_ENTRY", "a built-in reference names nothing", details={"ref": ref}
        ) from None

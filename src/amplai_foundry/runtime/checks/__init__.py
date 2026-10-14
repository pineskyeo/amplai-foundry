"""The ``check`` module kind (Work 034 OD-18, D-126; spec M-16..M-20).

- ``interface.py``: the kind's interface v1 (``CheckSpec``, ``CheckResult``) and its suite.
- ``builtin.py``: the built-in checks' declarations; ``lock.py``: ``checks.lock.json``.
- ``runner.py``: ``amplai checks run``; it loads each check through the module loader.
- one file per check implementation (``boundary.py``, ``kind_conformance.py``, ``removal.py``,
  ``secret_exposure.py``, ``policy.py``, ``tools.py``).

This package file imports nothing: the removal test's pytest plugin (``removal_plugin.py``) is
imported through it and must hide a module before any product code loads.
"""

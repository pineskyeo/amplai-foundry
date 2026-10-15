"""TB2 test dependencies baked into the task image at build time (Work 033 OD-15, TB2 option A).

Every TB2 ``tests/test.sh`` (89 of 89 at ``2fd12b8``) bootstraps its test runner at grading
time: 82 download ``uv`` with ``curl`` and run ``uvx -p PY -w PKG==V ... pytest ...``, 6 run
``pip install PKG==V ...`` then ``pytest``, 1 builds a ``uv venv``. Under V3 grading containment
(D-136, formerly D-135: network ``none``, read-only root, uid 65534) none of that can
download, so no runner starts (``runs/tb2-oracle-wave1.md``). Option A keeps D-136 unchanged
and moves the download to image build time, where the build has network:

- ``parse_test_entry`` reads the runner the unmodified ``test.sh`` asks for (Python version,
  pinned ``-w``/``pip install`` packages, extra index) and refuses anything it cannot pin
  (``git+`` URLs, shell variables, unpinned packages, more than one runner).
- ``bake_dockerfile_lines`` installs that runner into ``/opt/amplai-tb2-test`` (a uv-managed
  Python and a venv, owned by root, world-readable) with the ``uv`` binary copied from the
  digest-pinned ``UV_IMAGES`` entry of the task image platform, and puts a shim directory
  first on ``PATH``.
- The ``uvx`` shim runs the baked venv only when the arguments ask for exactly the baked Python
  and packages; any other request exits 127 with a message (never a silent substitute). The
  ``pip`` tasks get their pinned packages installed into the image's own interpreter at build
  time (what their ``pip install`` would have done), so ``pytest`` and ``python -m pytest`` work
  and the tests still import what the task installed.

``test.sh`` is not changed. Its ``apt-get``/``curl``/``pip install`` lines still run and fail
offline; none of the 89 scripts uses ``set -e``, so they fall through to the baked runner.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from amplai_foundry.runtime.errors import RuntimeFault

# ghcr.io/astral-sh/uv 0.9.5 (the version every TB2 test.sh installs), one per-platform manifest
# digest: the uv binary must match the task image platform (the classic builder resolves an index
# for the host, arm64 here, while every TB2 image is linux/amd64), else uv installs a Python for
# the wrong architecture.
UV_VERSION = "0.9.5"
UV_IMAGES = {
    "linux/amd64": "ghcr.io/astral-sh/uv:0.9.5"
    "@sha256:0f419824ea1810fe2a47af12aef8e8c39eabae8d19c728cd9612d18e17a98d17",
    "linux/arm64": "ghcr.io/astral-sh/uv:0.9.5"
    "@sha256:7bf0a77e82321b0f2019874f7ff40f2a58fa2ca851a816179931098d77e36d5e",
}
ROOT = "/opt/amplai-tb2-test"
VENV = f"{ROOT}/venv"
SHIM = f"{ROOT}/shim"
# no extras: the uvx shim accepts only these characters in a -w value
PINNED_REQ = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9.+!-]+")
PYTHON_VERSION = re.compile(r"3\.\d{1,2}")
INDEX_URL = re.compile(r"https://[A-Za-z0-9./_-]+")


@dataclass(frozen=True)
class RunnerDeps:
    """The test runner a ``test.sh`` asks for. ``kind`` is ``uvx`` or ``pip``."""

    kind: str
    packages: tuple[str, ...]
    python: str | None = None  # uvx -p; None for pip (the image's python3)
    index: str | None = None  # uvx --index (extra index, best-match strategy)
    index_strategy: str | None = None


class Unsupported(RuntimeFault):
    def __init__(self, message: str) -> None:
        super().__init__("TB2_TEST_DEPS", message)


def _joined(script: str) -> list[str]:
    return [line.strip() for line in script.replace("\\\n", " ").splitlines()]


def _pinned(packages: list[str]) -> tuple[str, ...]:
    for pkg in packages:
        if not PINNED_REQ.fullmatch(pkg):
            raise Unsupported(f"test dependency {pkg!r} is not a pinned PyPI requirement")
    if not packages:
        raise Unsupported("the test runner names no packages")
    return tuple(packages)


def _parse_uvx(argv: list[str]) -> RunnerDeps:
    python = index = strategy = None
    packages: list[str] = []
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        flag = argv[i]
        if i + 1 >= len(argv):
            raise Unsupported(f"uvx {flag} has no value")
        value = argv[i + 1]
        if flag in ("-p", "--python"):
            python = value
        elif flag in ("-w", "--with"):
            packages.append(value)
        elif flag == "--index":
            index = value
        elif flag == "--index-strategy":
            strategy = value
        else:
            raise Unsupported(f"uvx option {flag!r} is not supported")
        i += 2
    if i >= len(argv) or argv[i] != "pytest":
        raise Unsupported("uvx does not run pytest")
    if python is None or not PYTHON_VERSION.fullmatch(python):
        raise Unsupported(f"uvx Python {python!r} is not a pinned 3.x version")
    if index is not None and not INDEX_URL.fullmatch(index):
        raise Unsupported(f"uvx index {index!r} is not a plain https URL")
    if strategy not in (None, "unsafe-best-match"):
        raise Unsupported(f"uvx index strategy {strategy!r} is not supported")
    return RunnerDeps("uvx", _pinned(packages), python, index, strategy)


def parse_test_entry(script: str) -> RunnerDeps:
    """The runner one ``test.sh`` installs and runs; ``Unsupported`` when it cannot be pinned."""
    if "$" in "".join(line for line in _joined(script) if re.match(r"(uvx|pip3?) ", line)):
        raise Unsupported("the test runner line uses a shell variable")
    uvx, pip = [], []
    for line in _joined(script):
        if not re.match(r"(uvx|pip3?|uv)\s", line):
            continue  # comments, heredoc bodies and other commands are never tokenized
        try:
            words = shlex.split(line)
        except ValueError as exc:
            raise Unsupported(f"test runner line cannot be tokenized: {exc}") from exc
        if words[:1] == ["uvx"]:
            uvx.append(words[1:])
        elif words[:2] in (["pip", "install"], ["pip3", "install"]):
            pip.append(words[2:])
        elif words[:2] in (["uv", "venv"], ["uv", "run"], ["uv", "pip"]):
            raise Unsupported("uv project/venv runners are not supported")
    if len(uvx) + len(pip) != 1:
        raise Unsupported(f"expected one test runner, found {len(uvx) + len(pip)}")
    if uvx:
        return _parse_uvx(uvx[0])
    args = [a for a in pip[0] if a != "--break-system-packages"]
    if any(a.startswith("-") for a in args):
        raise Unsupported(f"pip install options {args!r} are not supported")
    if "pytest" not in {p.split("==")[0] for p in args}:
        raise Unsupported("pip install does not install pytest")
    return RunnerDeps("pip", _pinned(args))


def _uvx_shim(deps: RunnerDeps) -> str:
    """POSIX sh: exec the baked pytest when the uvx arguments ask for exactly ``deps``."""
    assert deps.python is not None
    want = " ".join(sorted(deps.packages))
    want_index = deps.index or ""
    want_strategy = deps.index_strategy or ""
    return "\n".join(
        [
            "#!/bin/sh",
            "# AMPLAI TB2 option A: uvx served offline from the image-baked runner.",
            'py=""; idx=""; strat=""; pkgs=""',
            'while [ $# -gt 0 ]; do case "$1" in',
            '  -p|--python) py="$2"; shift 2;;',
            '  -w|--with) case "$2" in *[!-A-Za-z0-9._=+!]*|"")',
            '    echo "amplai-tb2: uvx -w value is not a pinned requirement" >&2; exit 127;; esac',
            '    pkgs="$pkgs$2\n"; shift 2;;',
            '  --index) idx="$2"; shift 2;;',
            '  --index-strategy) strat="$2"; shift 2;;',
            '  -*) echo "amplai-tb2: uvx option $1 is not baked" >&2; exit 127;;',
            "  *) break;;",
            "esac; done",
            'got=$(printf "%s" "$pkgs" | sed "/^$/d" | LC_ALL=C sort'
            ' | tr "\\n" " " | sed "s/ $//")',
            f'if [ "$py" != {shlex.quote(deps.python)} ] || [ "$got" != {shlex.quote(want)} ] \\',
            f'   || [ "$idx" != {shlex.quote(want_index)} ] || [ "$strat" != '
            f'{shlex.quote(want_strategy)} ] || [ "$1" != pytest ]; then',
            '  echo "amplai-tb2: uvx request does not match the baked test runner" >&2; exit 127',
            "fi",
            "shift",
            f'exec {VENV}/bin/pytest "$@"',
            "",
        ]
    )


SHIM_CONTEXT = "amplai-tb2-shim"  # build-context directory COPYed to ``SHIM``


def shim_files(deps: RunnerDeps) -> dict[str, str]:
    """The build-context files of the shim directory, keyed by context-relative path."""
    if deps.kind == "uvx":
        return {f"{SHIM_CONTEXT}/uvx": _uvx_shim(deps)}
    if deps.kind == "pip":
        return {}  # the image interpreter is used, no shim
    raise Unsupported(f"unknown runner kind {deps.kind!r}")


def bake_dockerfile_lines(deps: RunnerDeps, platform: str) -> list[str]:
    """Dockerfile lines (run as root, classic builder or BuildKit) that install ``deps`` under
    ``ROOT`` (``uvx``: a managed Python, a venv and ``SHIM`` first on ``PATH``; ``pip``: the
    image interpreter's site-packages); the build context needs ``shim_files(deps)``."""
    shim_files(deps)  # validates kind
    uv_image = UV_IMAGES.get(platform)
    if uv_image is None:
        raise Unsupported(f"no pinned uv image for platform {platform!r}")
    pkgs = " ".join(shlex.quote(p) for p in deps.packages)
    env = f"UV_PYTHON_INSTALL_DIR={ROOT}/python UV_CACHE_DIR=/tmp/amplai-uv-cache"
    if deps.kind == "pip":
        # what test.sh's own ``pip install`` would do, into the image interpreter's site-packages
        # so that ``pytest`` and ``python -m pytest`` both import it (and the task's packages)
        install = (
            f"{env} {ROOT}/bin/uv pip install --system --break-system-packages "
            f'--python "$(command -v python3 || command -v python)" {pkgs}'
        )
        return [
            "# test runner baked at build time (TB2 option A; D-136 grading stays egress none)",
            f"COPY --from={uv_image} /uv /uvx {ROOT}/bin/",
            f"RUN {install} && rm -rf /tmp/amplai-uv-cache",
        ]
    assert deps.python is not None
    venv = f"uv venv --managed-python --python {shlex.quote(deps.python)} {VENV}"
    index = ""
    if deps.index:
        index = f" --index {shlex.quote(deps.index)}"
        if deps.index_strategy:
            index += f" --index-strategy {shlex.quote(deps.index_strategy)}"
    return [
        "# test runner baked at build time (TB2 option A; D-136 grading stays egress none)",
        f"COPY --from={uv_image} /uv /uvx {ROOT}/bin/",
        f"COPY {SHIM_CONTEXT}/ {SHIM}/",
        f"ENV PATH={SHIM}:{ROOT}/bin:$PATH",
        f"RUN {env} {venv} && "
        f"{env} uv pip install --python {VENV}/bin/python{index} {pkgs} && "
        f"rm -rf /tmp/amplai-uv-cache && chmod -R a+rX {ROOT} && chmod a+rx {SHIM}/*",
    ]

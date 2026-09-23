"""Every top-level module the app imports must have a COPY line in the Dockerfile.

The COPY list is maintained by hand, and a missing line builds fine but crashes
at startup with ``ModuleNotFoundError``. The required set comes from a clean
``import api`` in a subprocess, so pytest's own modules do not pollute it.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"

# Imports the app as the container does and reports the top-level modules that
# resolved to files inside the repo; stdlib and pip packages live elsewhere.
_PROBE = r"""
import api, sys, os, json
root = os.path.dirname(os.path.abspath(api.__file__))
sep = os.sep
skip_top = {".venv", ".venv-test", "venv", "env", "__pycache__", ".git", ".github"}
required = set()

def consider(path):
    if not path:
        return
    p = os.path.abspath(path)
    if p == root or p.startswith(root + sep):
        top = os.path.relpath(p, root).split(sep)[0]
        if top not in skip_top:
            required.add(top)

# A namespace package (no __init__.py) has __file__ == None, so __path__ is
# consulted too.
for mod in list(sys.modules.values()):
    consider(getattr(mod, "__file__", None))
    for entry in list(getattr(mod, "__path__", []) or []):
        consider(entry)

print("REQUIRED_MODULES_JSON=" + json.dumps(sorted(required)))
"""


def _norm(token: str) -> str:
    """Normalize a COPY source token to a bare package/file name for comparison."""
    token = token.strip().strip('"').strip("'")
    if token.startswith("./"):
        token = token[2:]
    return token.rstrip("/")


def _dockerfile_copy_sources(text: str) -> set:
    """The set of source paths copied into the image by the Dockerfile's COPY
    instructions (each COPY's tokens are ``<src>... <dst>``; the last is the dest)."""
    # Fold line-continuations into single logical lines first.
    logical, buf = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        logical.append(buf + line)
        buf = ""
    if buf:
        logical.append(buf)

    sources = set()
    for line in logical:
        parts = line.strip().split()
        if not parts or parts[0].upper() != "COPY":
            continue
        # Drop flags like --from=... / --chown=...; what's left is "<src>... <dst>".
        toks = [t for t in parts[1:] if not t.startswith("--")]
        if len(toks) < 2:
            continue
        for src in toks[:-1]:  # every token but the destination is a source
            sources.add(_norm(src))
    return sources


def _required_modules() -> list:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "Importing `api` in a clean subprocess failed, so the module graph could not "
        f"be probed.\n--- stderr ---\n{proc.stderr}"
    )
    for line in proc.stdout.splitlines():
        if line.startswith("REQUIRED_MODULES_JSON="):
            return json.loads(line[len("REQUIRED_MODULES_JSON="):])
    raise AssertionError(
        "Probe produced no REQUIRED_MODULES_JSON line.\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )


def test_every_imported_toplevel_module_is_copied_into_the_image():
    required = _required_modules()
    copied = _dockerfile_copy_sources(DOCKERFILE.read_text(encoding="utf-8"))

    missing = sorted(m for m in required if m not in copied)

    def _copy_line(name: str) -> str:
        return f"COPY {name} ." if name.endswith(".py") else f"COPY {name} ./{name}"

    assert not missing, (
        "These top-level modules are imported by the app but are NOT copied into "
        "the Docker image: it would build, then crash at startup with "
        "ModuleNotFoundError. Add to the Dockerfile:\n"
        + "\n".join(f"    {_copy_line(m)}" for m in missing)
    )

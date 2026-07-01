"""Guard: every top-level module the app imports must be COPY'd into the image.

The Dockerfile builds the runtime image with one ``COPY <pkg> ./<pkg>`` line per
local top-level package (``core``, ``resolvers``, ``metadata_engine``, …) plus
``COPY api.py .``. That list is maintained by hand, so it's easy to add a new
top-level package (e.g. ``web/``) and forget the matching COPY — the image then
builds fine but crashes at startup with ``ModuleNotFoundError`` the moment
``uvicorn api:app`` imports it.

This test makes that mistake a red CI gate instead of a failed deploy. It derives
the set of top-level modules the app *actually* imports from a clean ``import api``
(run in a subprocess so pytest's own modules don't pollute the graph), then asserts
each one has a COPY line in the Dockerfile. Add a package and import it, and this
test starts requiring its COPY automatically — no denylist to maintain.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"

# Snippet run in a fresh interpreter: import the app exactly as the container does,
# then report the top-level modules that resolved to files inside the repo (i.e.
# the ones that must be shipped in the image — stdlib and pip deps live elsewhere).
_PROBE = r"""
import api, sys, os, json
root = os.path.dirname(os.path.abspath(api.__file__))
sep = os.sep
skip_top = {".venv", ".venv-test", "venv", "env", "__pycache__", ".git", ".github"}
required = set()

def consider(path):
    # Record the top-level repo dir/file a loaded module lives in, if any. Uses
    # abspath so both "web/routes/x.py" -> "web" and the entrypoint "api.py" work.
    if not path:
        return
    p = os.path.abspath(path)
    if p == root or p.startswith(root + sep):
        top = os.path.relpath(p, root).split(sep)[0]
        if top not in skip_top:
            required.add(top)

# Walk EVERY loaded module (submodules included) and consult both __file__ and
# __path__ — a namespace package (no __init__.py, e.g. metadata_engine) has
# __file__ == None, so its dir is only discoverable via __path__ / its submodules.
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
        "Importing `api` in a clean subprocess failed — the module graph could not "
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
    """Fail loudly (before build/deploy) if the app imports a top-level module the
    Dockerfile never copies — the classic "forgot the COPY line" deploy breaker."""
    required = _required_modules()
    copied = _dockerfile_copy_sources(DOCKERFILE.read_text(encoding="utf-8"))

    missing = sorted(m for m in required if m not in copied)

    def _copy_line(name: str) -> str:
        # A top-level module file (api.py) is copied as a file; a package as a dir.
        return f"COPY {name} ." if name.endswith(".py") else f"COPY {name} ./{name}"

    assert not missing, (
        "These top-level modules are imported by the app but are NOT copied into "
        "the Docker image — it would build, then crash at startup with "
        "ModuleNotFoundError. Add to the Dockerfile:\n"
        + "\n".join(f"    {_copy_line(m)}" for m in missing)
    )

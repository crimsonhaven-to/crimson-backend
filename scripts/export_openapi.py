"""Dump the live FastAPI OpenAPI document to ``openapi.json`` (repo root).

Run it to refresh the published API description::

    python scripts/export_openapi.py

The frontend can codegen a typed client from this with::

    npx openapi-typescript openapi.json -o src/api-types.ts

Most read endpoints stream gzipped/NDJSON bodies via custom ``Response`` objects
(so FastAPI can't introspect their response *bodies*), but the document still
captures every path, method, path/query parameter and auth requirement — a
discoverable, version-controllable contract. The exact response *body* shape of
the one protocol the client is most coupled to (the /watch NDJSON line) is pinned
separately + machine-checked in ``core/contracts.py``.
"""

from __future__ import annotations

import json
import os
import sys

# Importing api.py is safe offline (no DB/network until the lifespan runs); a
# placeholder secret keeps the proxy signer modules importable.
os.environ.setdefault("PROXY_SECRET", "export-only-placeholder")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    from api import app

    doc = app.openapi()
    out = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "openapi.json"
    )
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"wrote {out} ({len(doc.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()

"""Write the FastAPI OpenAPI document to ``openapi.json`` at the repo root.

    python scripts/export_openapi.py
    npx openapi-typescript openapi.json -o src/api-types.ts   # frontend types

Most read endpoints return custom ``Response`` objects, so the document has
paths, methods, parameters and auth but not their body shapes. The /watch NDJSON
body is pinned separately in ``core/contracts.py``.
"""

from __future__ import annotations

import json
import os
import sys

# Importing api.py touches no DB or network until the lifespan runs. The
# placeholder secret spares the signer's random-secret warning.
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

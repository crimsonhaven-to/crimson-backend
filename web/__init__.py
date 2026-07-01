"""The HTTP layer for the Crimson backend.

``api.py`` used to be one ~3,500-line module holding the FastAPI app, every route,
the scrape/resolve pipeline, the DB helpers and the injected engine handlers. This
package splits that surface up by concern while keeping ``api.py`` as the single
assembler:

* ``web.context``        — the process-wide singletons (db engine, stores).
* ``web.queries``        — pure DB read helpers over the mapping/catalogue tables.
* ``web.serialization``  — gzip-aware JSON response helpers.
* ``web.util``           — small request/format helpers shared across routes.
* ``web.pipeline``       — the scrape -> resolve engine + the NDJSON /watch stream.
* ``web.warmup``         — the continue-watching pre-cache handler.
* ``web.admin_handlers`` — the runtime/system + source-health handlers the admin
                           router pulls in via dependency injection.
* ``web.routes``         — the ``APIRouter``s, grouped by concern.

Every module here forms a strict DAG: nothing under ``web`` imports ``api`` back,
so ``api.py`` can import all of it without a cycle.
"""

"""The HTTP layer, split by concern with ``api.py`` as the assembler.

* ``web.context``        process-wide singletons (db engine, stores)
* ``web.queries``        DB read helpers over the mapping/catalogue tables
* ``web.serialization``  gzip-aware JSON response helpers
* ``web.util``           request/format helpers shared across routes
* ``web.pipeline``       the scrape/resolve engine and the NDJSON /watch stream
* ``web.warmup``         the continue-watching pre-cache handler
* ``web.admin_handlers`` the system and source-health handlers the admin router
                         pulls in by injection
* ``web.routes``         the ``APIRouter``s, grouped by concern

These form a strict DAG: nothing under ``web`` imports ``api`` back, so api.py
can import all of it without a cycle.
"""

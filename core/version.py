"""Single source of truth for the API version + this replica's boot time.

Lifted out of ``api.py`` so the route/handler modules that report them (the root
greeting, the admin system snapshot) can import them without importing the whole
app — avoiding a circular import back through ``api.py``.
"""

import time

# Fed to the FastAPI app metadata (OpenAPI/docs) and the "/" root greeting.
VERSION = "16.3.1"

# Wall-clock at process start — the admin dashboard derives this replica's uptime
# from it. Module-load time is close enough to "boot" for an operator metric.
PROCESS_STARTED_AT = time.time()

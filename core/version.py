"""API version and this replica's boot time.

Separate from api.py so the modules that report them can import them without
pulling in the whole app.
"""

import time

# Fed to the FastAPI app metadata and the "/" root greeting.
VERSION = "18.1.9"

# The dashboard derives uptime from this. Module-load time is close enough to
# boot for an operator metric.
PROCESS_STARTED_AT = time.time()

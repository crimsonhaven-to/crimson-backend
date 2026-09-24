"""API version and this replica's boot time, importable without the whole app."""

import time

VERSION = "18.4.0"

# Module-load time is close enough to boot for the dashboard's uptime.
PROCESS_STARTED_AT = time.time()

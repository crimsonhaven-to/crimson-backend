"""Anonymous per-source resolve telemetry.

Once resolving moved into the client/extension (New System), the server lost
visibility into which sources actually work for viewers — the `source_health`
canary probes from the backend, which no longer reflects the real client+edge
path. This engine ingests tiny, anonymous per-source outcome beacons from the
client engine (source label + ok/fail, no titles, no user, no IPs) and aggregates
them by day so the admin dashboard can show real success rates over time.
"""

from .db import TelemetryStore

__all__ = ["TelemetryStore"]

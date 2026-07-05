# download_engine — admin-only background downloader. Takes a plain http/https URL
# or a magnet/.torrent link from the Admin Dashboard and fetches it in the
# background via an aria2c sidecar, landing the finished media under
# ``<root>/crimson-downloads/`` on the first *download-enabled* local source with
# enough free space. Once on disk, the existing local_engine library scanner
# surfaces it like any other on-disk title — there is no separate playback path.
#
# The DB store (download_jobs) lives in .db; filesystem helpers (target picking,
# staging, the leech-only publish move) live in .fs; the aria2 JSON-RPC transport
# in .aria2; the background poll worker + pause/resume/cancel controls in .manager.
from .db import DownloadStore

__all__ = ["DownloadStore"]

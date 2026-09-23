"""Running ffmpeg and ffprobe, shared by the video cache and Local transcoding."""

import asyncio
import shutil
from functools import cache
from typing import Optional


# The binaries are baked into the image, so one lookup per process is enough.
@cache
def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


@cache
def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


async def run(
    args: list[str], *, timeout: float, capture_stdout: bool = False
) -> tuple[Optional[int], bytes, list[str]]:
    """``(returncode, stdout, stderr_lines)``. The return code is None when the
    process was killed for running past ``timeout``, so a hung input can never pin
    a worker."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, b"", []
    lines = (err or b"").decode("utf-8", errors="replace").strip().splitlines()
    return proc.returncode, out or b"", lines

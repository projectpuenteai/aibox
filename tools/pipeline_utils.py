"""Shared helpers for local data-prep and indexing scripts."""

import math
import os
import sys


def count_lines(filepath: str) -> int:
    """Fast line count without loading the whole file into memory."""
    count = 0
    with open(filepath, "rb") as f:
        while True:
            buf = f.raw.read(1024 * 1024)
            if not buf:
                break
            count += buf.count(b"\n")
    return count


def format_eta(seconds: float) -> str:
    """Format seconds into a compact ETA string."""
    if seconds <= 0 or not math.isfinite(seconds):
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def set_low_priority() -> None:
    """Set the current process to below-normal priority when supported."""
    try:
        if sys.platform == "win32":
            import ctypes

            below_normal_priority_class = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(),
                below_normal_priority_class,
            )
        else:
            os.nice(10)
    except Exception:
        pass

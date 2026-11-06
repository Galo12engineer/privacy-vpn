"""Collection of AGGREGATE node metrics. No per-user data. Best-effort, no external dependencies."""

from __future__ import annotations

import time


def read_mem_pct() -> int | None:
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        if total <= 0:
            return None
        return round(100 * (total - avail) / total)
    except (OSError, ValueError, IndexError):
        return None


def read_cpu_pct(sample: float = 0.1) -> int | None:
    try:
        a = _cpu_times()
        time.sleep(sample)
        b = _cpu_times()
        idle = b[3] - a[3]
        total = sum(b) - sum(a)
        if total <= 0:
            return None
        return round(100 * (1 - idle / total))
    except (OSError, ValueError, IndexError):
        return None


def _cpu_times() -> list[int]:
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:8]
    return [int(p) for p in parts]


def collect(*, peer_count: int | None = None) -> dict:
    return {
        "cpu_pct": read_cpu_pct(),
        "mem_pct": read_mem_pct(),
        "peers": peer_count,
    }

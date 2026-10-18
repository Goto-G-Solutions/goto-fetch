"""How long select() takes on 100,000 records, the case paths are cached for.

Run with: uv run python benchmarks/select_paths.py

Without the cache on paths._alternatives this took about 1.0s on an M-series
Mac; with it, about 0.5s. Rerun after changing how paths are parsed.
"""

import time

from goto_fetch import select

RECORDS = [
    {"id": i, "user": {"name": f"u{i}", "tags": [{"n": 1}]}, "meta": {"a": i}}
    for i in range(100_000)
]
COLUMNS = ["id", "user.name", "user.tags[*].n", "meta.a|meta.b", "missing"]


def best_of(runs: int = 3) -> float:
    """The fastest of `runs` timings of select over RECORDS, in seconds."""
    timings = []
    for _ in range(runs):
        started = time.perf_counter()
        select(RECORDS, COLUMNS)
        timings.append(time.perf_counter() - started)
    return min(timings)


if __name__ == "__main__":
    print(f"select: {best_of():.2f}s for {len(RECORDS):,} records")

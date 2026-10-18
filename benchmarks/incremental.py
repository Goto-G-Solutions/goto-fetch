"""How long Incremental.track takes on 100,000 records with ISO timestamps.

Run with: uv run python benchmarks/incremental.py

Each record's timestamp is read into a comparable key once, then keys
are compared. Reading the text again at every comparison (about six times
a record) took about 0.17s on an M-series Mac; reading it once takes
about 0.08s. Rerun after changing how values are compared.
"""

import time

from goto_fetch import Incremental

RECORDS = [
    {
        "id": i,
        "updated_at": f"2026-01-{1 + i // 5000:02d}T{(i // 60) % 24:02d}:{i % 60:02d}:00Z",
    }
    for i in range(100_000)
]
STATE = {"last": RECORDS[50_000]["updated_at"], "seen": []}


def best_of(runs: int = 3) -> float:
    """The fastest of `runs` timings of one tracked pass over RECORDS, in seconds."""
    best = float("inf")
    for _ in range(runs):
        since = Incremental("updated_at", state=STATE)
        started = time.perf_counter()
        for _ in since.track(RECORDS):
            pass
        best = min(best, time.perf_counter() - started)
    return best


if __name__ == "__main__":
    print(f"{best_of():.3f}s for {len(RECORDS):,} records")

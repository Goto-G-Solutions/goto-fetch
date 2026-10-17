"""How long Changes takes on 100,000 records, and how big its state gets.

Run with: uv run python benchmarks/changes.py

It times a first run (every record new) and a second run (every record
unchanged), and the size of the saved state as JSON. On an M-series Mac,
each run took about 0.4s, and the state was 4.5 MB (45 bytes a record),
so a million records means about 4s and 45 MB. Rerun after changing how
records are fingerprinted.
"""

import json
import time

from api_explorer import Changes

RECORDS = [
    {
        "id": i,
        "name": f"product {i}",
        "price": i % 500 / 10,
        "tags": ["a", "b"],
        "stock": {"warehouse": i % 7, "fetched_at": "2026-01-02T10:00:00Z"},
    }
    for i in range(100_000)
]


def timed_run(state: dict | None) -> tuple[float, Changes]:
    """Seconds for one run of Changes over RECORDS, and the Changes."""
    changes = Changes("id", ignore=["stock.fetched_at"], state=state)
    started = time.perf_counter()
    for _ in changes.track(RECORDS):
        pass
    return time.perf_counter() - started, changes


if __name__ == "__main__":
    first_seconds, first = timed_run(None)
    second_seconds, _ = timed_run(first.state)
    size = len(json.dumps(first.state))
    print(f"first run:  {first_seconds:.2f}s for {len(RECORDS):,} records")
    print(f"second run: {second_seconds:.2f}s")
    print(
        f"state:      {size / 1e6:.1f} MB as JSON, {size / len(RECORDS):.0f} bytes a record"
    )

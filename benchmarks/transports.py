"""How many sequential JSON pages a second each transport sends.

Run with: uv run python benchmarks/transports.py [URL]

URL should serve a JSON page fast, so the server doesn't hide the
transports' differences; the figures below came from a small Go server
answering a 20 KB page (and a 1.3 MB one), 2,000 requests, best of three,
on an M-series Mac. Without a URL, the script serves a page itself with
Python's own server, which is slow enough to narrow the gap.

    Urllib3Transport   ~8,300 small pages/s   ~5,600 large pages/s
    Httpx2Transport    ~6,100 small pages/s   ~2,200 large pages/s

Against an API's own response time (often 50 to 500 ms a page), either is
a small part of a run. Rerun after changing a transport.
"""

import sys
import time

from goto_fetch import Httpx2Transport, Urllib3Transport, http
from goto_fetch.testing import serve

PAGE = {
    "results": [
        {"id": i, "name": "product name here", "price": 12.5} for i in range(150)
    ]
}
N = 2000


def best_of(transport: http.Transport, url: str, runs: int = 3) -> float:
    """Pages a second, the best of `runs` runs of N sequential GETs."""
    timeout = http.Timeout.of(10)
    for _ in range(50):
        transport.send(http.Request("GET", url), timeout)
    best = float("inf")
    for _ in range(runs):
        started = time.perf_counter()
        for _ in range(N):
            transport.send(http.Request("GET", url), timeout)
        best = min(best, time.perf_counter() - started)
    return N / best


def main(url: str) -> None:
    for name, make in [
        ("Urllib3Transport", Urllib3Transport),
        ("Httpx2Transport", Httpx2Transport),
    ]:
        transport = make()
        print(f"{name:18} {best_of(transport, url):8.0f} pages/s")
        transport.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        with serve(lambda request: http.Response(200, json=PAGE)) as base:
            main(f"{base}/page")

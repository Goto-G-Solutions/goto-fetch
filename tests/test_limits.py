"""RateLimit and Limited: keeping to a rate, across requests, retries, logins and threads.

A fake clock and sleep make the waits exact and instant; one test uses
real threads, to show the budget holds under contention.
"""

import itertools
import threading
import time

import pytest

from api_explorer import Client, Limited, PasswordLogin, RateLimit, Retry, TokenRefresh
from api_explorer.testing import TokenServer, check_transport, flaky, next_link_api


class FakeTime:
    """A clock that moves only when something sleeps."""

    def __init__(self):
        self.now, self.slept = 0.0, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(round(seconds, 6))
        self.now += seconds


def limit_with(per_second, **options):
    fake = FakeTime()
    return RateLimit(per_second, clock=fake.clock, sleep=fake.sleep, **options), fake


def test_requests_are_spaced_to_the_rate():
    limit, _ = limit_with(4)
    waits = [limit.wait() for _ in range(4)]
    assert waits == [0.0, 0.25, 0.25, 0.25]


def test_a_burst_goes_at_once_then_the_rate_holds():
    limit, _ = limit_with(2, burst=3)
    assert [limit.wait() for _ in range(5)] == [0.0, 0.0, 0.0, 0.5, 0.5]


def test_a_quiet_spell_saves_up_no_more_than_the_burst():
    limit, fake = limit_with(1, burst=2)
    limit.wait()
    fake.now += 60  # a long quiet spell
    assert [limit.wait() for _ in range(3)] == [0.0, 0.0, 1.0]


def test_every_request_a_client_sends_counts_pages_retries_and_logins():
    limit, fake = limit_with(10)
    server = flaky(TokenServer(), 503)  # one 503 first, then the server
    auth = TokenRefresh("/token/refresh/", login=PasswordLogin("/token/", "me", "pw"))
    client = Client(
        "http://api.test/api",
        auth=auth,
        transport=Limited(server, limit),
        retry=Retry(backoff=0),
    )
    assert list(client.records("/items/")) == [{"id": 1}]
    # The 503 (on the login), the login again, and the page: three
    # requests, the second and third each a tenth of a second apart.
    assert len(server.calls) == 3
    assert fake.slept == [0.1, 0.1]


def test_one_rate_limit_shared_by_two_clients_is_one_budget():
    limit, fake = limit_with(2)
    first = Client("http://api.test/api", transport=Limited(next_link_api(1), limit))
    second = Client("http://api.test/api", transport=Limited(next_link_api(1), limit))
    list(first.records("/a/"))
    list(second.records("/b/"))
    assert fake.slept == [0.5]


def test_threads_sharing_a_limit_never_go_faster_than_the_rate():
    limit = RateLimit(50)  # real time: 20 ms apart
    moments = []
    lock = threading.Lock()

    def take():
        limit.wait()
        with lock:
            moments.append(time.monotonic())

    threads = [threading.Thread(target=take) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    moments.sort()
    gaps = [b - a for a, b in itertools.pairwise(moments)]
    assert min(gaps) > 0.015  # a little under 20 ms, for timer jitter


def test_a_limited_transport_keeps_every_transport_promise():
    from api_explorer import Urllib3Transport

    check_transport(lambda: Limited(Urllib3Transport(), RateLimit(1000)))


@pytest.mark.parametrize(
    ("options", "message"),
    [({"per_second": 0}, "per_second"), ({"per_second": 1, "burst": 0}, "burst")],
    ids=["no-rate", "no-burst"],
)
def test_a_limit_that_can_t_work_is_refused(options, message):
    with pytest.raises(ValueError, match=message):
        RateLimit(**options)

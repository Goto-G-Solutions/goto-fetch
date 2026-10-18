# Retries

A retry policy decides whether a failed request is tried again, and after how long. The built-in one, `Retry`, is on by default.

## What it handles for you

`Retry()` tries each request up to 3 times:

- **retried:** network errors, timeouts, connections dropped mid-response, 408, 429 and 5xx (except 501 and 505), a token endpoint's temporary failures, and a temporary problem a response check finds;
- **only for idempotent methods** (GET, HEAD, OPTIONS, PUT, DELETE), since repeating a POST could repeat a change; pass `retry=True` to a call that only reads;
- **waits that grow** (1s, 2s, 4s... with jitter, never more than `max_backoff`), or the server's `Retry-After`, if longer. A `Retry-After` longer than `max_backoff` fails at once rather than block.

## Examples

```python
from goto_fetch import ApiError, Retry
from goto_fetch.testing import flaky, next_link_api, replies

# two 503s, then the pages: the run carries on
api = flaky(next_link_api(5), 503, 503)
assert list(api.client(retry=Retry(backoff=0)).records("/items/")) == [0, 1, 2, 3, 4]

# a 404 is final: no retry
api = replies(404)
try:
    api.client().get_json("/missing/")
except ApiError as error:
    assert error.status == 404
assert len(api.calls) == 1

# more tries, longer waits, and 404 treated as temporary
policy = Retry(attempts=5, backoff=2, max_backoff=60, statuses=Retry().statuses | {404})
```

A call says `retry=False` to never retry, or `retry=True` to retry whatever the method:

```python skip
client.request("POST", "/search/", json={"q": "x"}, retry=True)
```

A policy of your own is any object with `wait(failure)`, returning seconds or `None` to stop; see [build-your-own](build-your-own.md) and check it with `check_retry_policy`.

A server that can't take a fast run needs a rate limit rather than more retries: `Client(rate_limit=RateLimit(2))` spaces every request, retries and token requests included, and one `RateLimit` shared by several clients is one budget.

```python
import time
from goto_fetch import Client, RateLimit

api = next_link_api(30, page_size=10)  # three pages
client = Client("http://api.test/api", transport=api, rate_limit=RateLimit(20))
started = time.monotonic()
assert len(list(client.records("/items/"))) == 30
assert time.monotonic() - started >= 0.1  # three requests, at most 20 a second
```

## Traps

- Retries multiply time: 3 tries with growing waits can turn one slow endpoint into minutes. Watch the "retry" warnings ([logging](logging.md)).
- A POST that only reads (a search) needs `retry=True`, or it fails on the first hiccup.

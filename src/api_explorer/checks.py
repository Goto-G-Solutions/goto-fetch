"""Response checks: catch a successful response that reports a problem in its body.

Some APIs answer HTTP 200 and put the error in the body, so a run would
carry on with a partial or empty page and report success. A check looks at
each successful response and says whether it really failed.
"""

import json
from collections.abc import Callable

import httpx2

from .errors import Problem
from .paths import get_path

ResponseCheck = Callable[[httpx2.Response], "Problem | str | None"]
"""Looks at a successful response: None if it is fine, else what is wrong.

A message (str) is a permanent problem; return Problem(message,
temporary=True) for one worth retrying, such as a rate limit reported in
the body. Pass it as Client(check=...).
"""


def error_at(path: str = "error|errors", *, temporary: bool = False) -> ResponseCheck:
    """A check that reports a problem when the JSON body holds something at `path`.

    error_at() catches {"error": "quota exceeded"} and {"errors": [...]},
    and lets {"errors": []} and {"error": null} through: an empty or null
    value means no error. `path` follows get_path, fallbacks included. With
    temporary=True, the problem is worth retrying. A body that isn't JSON
    isn't this check's concern; reading the page reports that.
    """

    def check(response: httpx2.Response) -> Problem | None:
        try:
            body = json.loads(response.content)
        except ValueError:
            return None
        found = get_path(body, path)
        if found in (None, "", [], {}):
            return None
        detail = json.dumps(found, default=str)
        return Problem(f"{path!r} holds {detail[:200]}", temporary=temporary)

    return check

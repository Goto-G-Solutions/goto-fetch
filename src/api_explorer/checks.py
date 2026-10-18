"""Response checks: catch a successful response that reports a problem in its body.

Some APIs answer HTTP 200 and put the error in the body, so a run would
carry on with a partial or empty page and report success. A check looks at
each successful response and says whether it really failed.
"""

import json
from collections.abc import Callable

from .decode import Decoder, json_decoder
from .errors import Problem
from .http import Response
from .paths import get_path

ResponseCheck = Callable[[Response], "Problem | str | None"]
"""Looks at a successful response: None if it is fine, else what is wrong.

A message (str) is a permanent problem, the short form of
Problem(message); return Problem(message, temporary=True) for one worth
retrying, such as a rate limit reported in the body. Pass it as
Client(check=...).
"""


def means_no_error(value: object) -> bool:
    """Whether a value at an error path means "no error".

    null, false, 0 and an empty text, list or object do: many APIs send
    "error": false or "errors": [] on success. Anything else, true
    included, is an error.
    """
    if value is None or value is False:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0
    return value in ("", [], {})


def as_problem(verdict: "Problem | str | None") -> Problem | None:
    """A check's verdict as a Problem, or None: a message becomes a permanent one."""
    return Problem(verdict) if isinstance(verdict, str) else verdict


def error_at(
    path: str = "error|errors",
    *,
    temporary: bool = False,
    decode: Decoder | None = None,
) -> ResponseCheck:
    """A check that reports a problem when the JSON body holds something at `path`.

    error_at() catches {"error": "quota exceeded"} and {"errors": [...]},
    and lets through what means no error: null, false, 0, and an empty
    text, list or object ({"error": false} and {"errors": []} pass).
    `path` follows get_path, fallbacks included. With temporary=True, the
    problem is worth retrying. `decode` parses the body (json_decoder() by
    default, so msgspec when installed); pass the client's own for the same
    reading. A body that isn't JSON isn't this check's concern; reading the
    page reports that.
    """
    parse = decode or json_decoder()

    def check(response: Response) -> Problem | None:
        try:
            body = parse(response.content)
        except ValueError:
            return None
        found = get_path(body, path)
        if means_no_error(found):
            return None
        detail = json.dumps(found, default=str)
        return Problem(f"{path!r} holds {detail[:200]}", temporary=temporary)

    return check

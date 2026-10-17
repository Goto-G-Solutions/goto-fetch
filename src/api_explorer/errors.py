"""The one exception this package raises, and redaction of secrets in URLs."""

import re
from dataclasses import dataclass
from urllib.parse import unquote_plus, urlsplit, urlunsplit

SENSITIVE = re.compile(
    r"key|token|secret|pass(word|wd)?|sig(nature)?|auth|credential|session",
    re.IGNORECASE,
)
"""Query parameter names whose values are hidden in messages and logs."""

_URL = re.compile(r"https?://[^\s'\"<>]+")


def redact_url(url: str) -> str:
    """Return `url` with the values of sensitive query parameters replaced by "***".

    A parameter is sensitive when its name matches SENSITIVE, such as
    api_key, access_token, client_secret or signature. Everything else is
    kept exactly as written, encoding included, so a redacted URL still
    shows what was sent.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url

    def hide(pair: str) -> str:
        name, equals, _ = pair.partition("=")
        return (
            f"{name}=***" if equals and SENSITIVE.search(unquote_plus(name)) else pair
        )

    query = "&".join(hide(pair) for pair in parts.query.split("&"))
    return urlunsplit(parts._replace(query=query))


def redact(text: str) -> str:
    """Redact every URL found in `text` (see redact_url)."""
    return _URL.sub(lambda match: redact_url(match.group(0)), text)


class ApiError(Exception):
    """A request that failed for good.

    Raised after the retries run out, or at once for a failure that retrying
    can't fix, such as a 404. `url` is the URL that failed, and `status` is
    the HTTP status code when there was a response.

    Secrets in URLs, such as an API key passed as ?api_key=..., are
    redacted in the message and in `url`, so an error can be logged or
    shown without leaking them.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(redact(message))
        self.url = None if url is None else redact_url(url)
        self.status = status


@dataclass(frozen=True)
class Problem:
    """A problem a successful response reports in its body, found by a response check.

    Some APIs answer 200 and put the error in the body, such as
    {"error": "quota exceeded"}. `temporary` marks one worth retrying, such
    as a rate limit reported that way; the retry policy decides. A
    permanent one fails the request at once.
    """

    message: str
    temporary: bool = False

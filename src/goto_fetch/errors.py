"""The package's own exception, the Problem a response check reports, and redaction of secrets in URLs."""

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
    """Return `url` with its secrets replaced by "***".

    The secrets are what sits before the "@" (a password, as in
    user:password@host, or a lone name, which is often a token:
    token@host), and the values of sensitive parameters, whose names match
    SENSITIVE, such as api_key, access_token, client_secret or signature,
    in the query or in a fragment written like one (#access_token=...).
    Everything else is kept exactly as written, encoding included, so a
    redacted URL still shows what was sent.

    A URL that can't be read, such as one with a port that isn't a number,
    comes back as "***" alone: its secrets can't be found, so none of it
    is shown. This never raises.
    """
    try:
        parts = urlsplit(url)
        _ = parts.port  # an unreadable port raises only when asked for
    except ValueError:
        return "***"
    userinfo, at, host = parts.netloc.rpartition("@")
    if at:
        user, colon, _ = userinfo.partition(":")
        parts = parts._replace(netloc=f"{user}:***@{host}" if colon else f"***@{host}")
    if parts.query:
        parts = parts._replace(query=_hide_values(parts.query))
    if "=" in parts.fragment:
        parts = parts._replace(fragment=_hide_values(parts.fragment))
    return urlunsplit(parts)


def _hide_values(pairs: str) -> str:
    """`pairs` (name=value&...) with the values of sensitive names replaced."""

    def hide(pair: str) -> str:
        name, equals, _ = pair.partition("=")
        return (
            f"{name}=***" if equals and SENSITIVE.search(unquote_plus(name)) else pair
        )

    return "&".join(hide(pair) for pair in pairs.split("&"))


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

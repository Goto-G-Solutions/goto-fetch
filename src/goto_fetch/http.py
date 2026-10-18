"""The package's own HTTP vocabulary: requests, responses, headers, and the Transport interface.

The rest of the package speaks only these types, so the library that does
the sending is a choice, not a dependency: a Transport takes a Request and
gives back a Response. Two ship with the package (Urllib3Transport, the
default, and Httpx2Transport, the "httpx2" extra), and anything with the
same two methods works (testing.check_transport checks one).

The shapes follow httpx's where they meet, Response(200, json=...) and
Request("GET", url, json=...), so code written against httpx reads the
same here. Only the standard library is used.
"""

import json as jsonlib
import re
from collections.abc import Generator, Iterable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import (
    parse_qsl,
    unquote_plus,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

from .errors import SENSITIVE

# One link of a Link header: <target> then attributes, up to the next link;
# quoted values may hold "," or ";".
_LINK = re.compile(r'<([^>]*)>((?:\s*;\s*[^;,=\s]+\s*(?:=\s*(?:"[^"]*"|[^;,]*))?)*)')
_ATTRIBUTE = re.compile(r';\s*([^;,=\s]+)\s*(?:=\s*(?:"([^"]*)"|([^;,]*)))?')


class URL(str):
    """A URL that is also plain text, with its parts at hand: path, query, params."""

    @property
    def scheme(self) -> str:
        """The scheme, such as "https"."""
        return urlsplit(self).scheme

    @property
    def host(self) -> str:
        """The host name, without port."""
        return urlsplit(self).hostname or ""

    @property
    def path(self) -> str:
        """The path, such as "/api/items/"."""
        return urlsplit(self).path

    @property
    def query(self) -> str:
        """The query, without its "?", as written."""
        return urlsplit(self).query

    @property
    def params(self) -> dict[str, str]:
        """The query's parameters; of several with one name, the last."""
        return dict(parse_qsl(self.query, keep_blank_values=True))

    def get_list(self, name: str) -> list[str]:
        """Every value the query gives `name`, in order, such as ?status=a&status=b."""
        pairs = parse_qsl(self.query, keep_blank_values=True)
        return [value for key, value in pairs if key == name]

    def copy_set_param(self, name: str, value: object) -> "URL":
        """This URL with `name` set to `value`, replacing any it had, others kept."""
        return set_param(self, name, value)

    def copy_merge_params(self, params: Mapping[str, object]) -> "URL":
        """This URL with `params` set, replacing those it had by the same names."""
        url = self
        for name, value in params.items():
            url = set_param(url, name, value)
        return url


def set_param(url: str, name: str, value: object) -> URL:
    """`url` with query parameter `name` set to `value`, in place if it was there.

    The other parameters keep their order and their encoding, as written.
    """
    parts = urlsplit(url)
    pairs = [p for p in parts.query.split("&") if p] if parts.query else []
    encoded = _encode(name, value)
    kept, placed = [], False
    for pair in pairs:
        if _name_of(pair) == name:
            if not placed:
                kept.append(encoded)
                placed = True
            continue
        kept.append(pair)
    if not placed:
        kept.append(encoded)
    return URL(urlunsplit(parts._replace(query="&".join(kept))))


def is_absolute(url: str) -> bool:
    """Whether `url` names its own host ("https://h/x", "//h/x"), not a path under a base.

    "projects:list" is a path, though urlsplit would read "projects" as
    its scheme.
    """
    return url.startswith("//") or bool(_SCHEME.match(url))


_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://")


def join_url(base: str, endpoint: str) -> URL:
    """`endpoint` under `base`: "items/" under "https://h/api" is "https://h/api/items/".

    An absolute endpoint is used as given (see is_absolute). A leading "/"
    on the endpoint doesn't climb out of the base path, and a query or
    fragment on the base is left behind, since the endpoint brings its own.
    """
    if is_absolute(endpoint):
        return URL(endpoint)
    root = urlsplit(base)._replace(query="", fragment="").geturl()
    # "./" keeps urljoin from reading "projects:list" as a scheme and a path.
    return URL(urljoin(root.rstrip("/") + "/", "./" + endpoint.lstrip("/")))


def _name_of(pair: str) -> str:
    return unquote_plus(pair.partition("=")[0])


def _text(value: object) -> str:
    """A parameter value as text: booleans as JSON writes them, None as nothing."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _encode(name: str, value: object) -> str:
    """`name=value` for a query or form; a list or tuple gives one pair per item."""
    values = value if isinstance(value, (list, tuple)) else [value]
    return urlencode([(name, _text(item)) for item in values])


class Headers(MutableMapping[str, str]):
    """HTTP headers: names matched without regard to case, as HTTP says.

    Built from a mapping or a list of (name, value) pairs, so a name sent
    twice (Set-Cookie, Link) keeps every value: get_list(name) gives them
    all, and headers[name] joins them with ", ".
    """

    def __init__(
        self, items: Mapping[str, str] | Iterable[tuple[str, str]] | None = None
    ) -> None:
        self._items: list[tuple[str, str]] = []
        if isinstance(items, Headers):
            pairs = items.raw()  # as pairs: items() would join repeats
        else:
            pairs = items.items() if isinstance(items, Mapping) else (items or [])
        for name, value in pairs:
            self._items.append((str(name), str(value)))

    def get_list(self, name: str) -> list[str]:
        """Every value sent under `name`, in order."""
        wanted = name.lower()
        return [value for key, value in self._items if key.lower() == wanted]

    def __getitem__(self, name: str) -> str:
        values = self.get_list(name)
        if not values:
            raise KeyError(name)
        return ", ".join(values)

    def __setitem__(self, name: str, value: str) -> None:
        wanted = name.lower()
        self._items = [(k, v) for k, v in self._items if k.lower() != wanted]
        self._items.append((name, str(value)))

    def __delitem__(self, name: str) -> None:
        wanted = name.lower()
        if not self.get_list(name):
            raise KeyError(name)
        self._items = [(k, v) for k, v in self._items if k.lower() != wanted]

    def __iter__(self) -> Iterator[str]:
        # One name per header, as first written, whatever the case of repeats.
        first: dict[str, str] = {}
        for key, _ in self._items:
            first.setdefault(key.lower(), key)
        return iter(first.values())

    def __len__(self) -> int:
        return len(dict.fromkeys(key.lower() for key, _ in self._items))

    def raw(self) -> list[tuple[str, str]]:
        """Every (name, value) pair, in order, repeats included."""
        return list(self._items)

    def __repr__(self) -> str:
        return f"Headers({self._items!r})"


MAX_REDIRECTS = 10
"""How many redirects a transport follows before giving back the last reply as it is."""


def credential_headers(headers: Headers) -> list[str]:
    """The names in `headers` that may carry a credential, as written.

    Those matching SENSITIVE (Authorization, X-API-Key, X-Auth-Token and
    the like: the same rule that hides query values), plus Cookie. A
    transport drops them from a request it sends on to another host after
    a redirect, so a credential never leaves the host it was meant for.
    """
    return [
        name for name in headers if name.lower() == "cookie" or SENSITIVE.search(name)
    ]


def same_host(url: str, other: str) -> bool:
    """Whether two URLs name one host: the same scheme, host name and port."""
    a, b = urlsplit(url), urlsplit(other)
    return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)


class Request:
    """One HTTP request: a method, a URL, headers, and a body of bytes.

    `params` merge into the URL's query, `json` becomes a JSON body,
    `data` a form (a mapping) or raw bytes, and `content` raw bytes; give
    at most one body. None means no body, as in httpx: a JSON null body
    is content=b"null".
    """

    def __init__(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        content: bytes | None = None,
        json: Any = None,
        data: Mapping[str, object] | bytes | None = None,
    ) -> None:
        bodies = [content is not None, json is not None, data is not None]
        if sum(bodies) > 1:
            raise ValueError("Give one of content, json or data, not several")
        self.method = method.upper()
        self.url = URL(url).copy_merge_params(params) if params else URL(url)
        self.headers = Headers(headers)
        self.content = b"" if content is None else content
        if json is not None:
            self.content = jsonlib.dumps(json).encode()
            self.headers.setdefault("Content-Type", "application/json")
        elif isinstance(data, Mapping):
            self.content = "&".join(_encode(k, v) for k, v in data.items()).encode()
            self.headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif data is not None:
            self.content = bytes(data)

    def __repr__(self) -> str:
        return f"<Request {self.method} {self.url}>"


class Response:
    """One HTTP response: a status, headers, the body as bytes, and the request it answers.

    A transport builds it from what the server sent, with the body already
    decompressed. `json=` and `text=` build one for tests, as
    Response(200, json={"ok": True}).
    """

    def __init__(
        self,
        status_code: int,
        *,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        content: bytes | None = None,
        text: str | None = None,
        json: Any = None,
        request: Request | None = None,
        url: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = Headers(headers)
        self.content = b"" if content is None else content
        if json is not None:
            self.content = jsonlib.dumps(json).encode()
            self.headers.setdefault("Content-Type", "application/json")
        elif text is not None:
            self.content = text.encode()
            self.headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        self.request = request or Request("GET", url or "http://unknown/")
        self.url = URL(url) if url else self.request.url

    @property
    def is_success(self) -> bool:
        """Whether the status is 2xx."""
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        """The body as text, in the charset the Content-Type names (UTF-8 if none)."""
        found = re.search(
            r'charset="?([\w.:-]+)', self.headers.get("Content-Type", ""), re.IGNORECASE
        )
        try:
            return self.content.decode(found[1] if found else "utf-8", errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """The body parsed as JSON; raises ValueError if it isn't."""
        return jsonlib.loads(self.content)

    @property
    def links(self) -> dict[str, dict[str, str]]:
        """The Link header's links, by rel: {"next": {"url": ..., "rel": "next"}}.

        Follows RFC 8288 where it matters for paging: attribute names in any
        case, quoted values (which may hold ";" or ","), and a rel naming
        several relations ("next last"), which files the link under each.
        """
        found: dict[str, dict[str, str]] = {}
        for header in self.headers.get_list("Link"):
            for target, attributes in _LINK.findall(header):
                pairs = {
                    name.lower(): quoted if quoted or not bare else bare
                    for name, quoted, bare in _ATTRIBUTE.findall(attributes)
                }
                rels = pairs.get("rel", "").split() or [target]
                for rel in rels:
                    found.setdefault(rel, {"url": target, **pairs, "rel": rel})
        return found

    def __repr__(self) -> str:
        return f"<Response [{self.status_code}]>"


@dataclass(frozen=True)
class Timeout:
    """How long to wait: `connect` seconds to make a connection, `read` seconds for each read.

    None means no limit. Timeout.of(20) waits 20 seconds at each stage;
    Timeout(connect=5, read=60) gives up quickly when a server is down but
    waits for a slow page.
    """

    connect: float | None = None
    read: float | None = None

    @classmethod
    def of(cls, value: "float | Timeout | None") -> "Timeout":
        """`value` as a Timeout: a number applies to both stages, None to neither."""
        if isinstance(value, Timeout):
            return value
        return cls(value, value)

    def __post_init__(self) -> None:
        for stage, seconds in (("connect", self.connect), ("read", self.read)):
            if seconds is not None and seconds < 0:
                raise ValueError(
                    f"The {stage} timeout can't be negative, got {seconds}"
                )


class TransportError(Exception):
    """A request that got no response: a final failure, unless it is a NetworkError."""


class NetworkError(TransportError):
    """A failure worth trying again: refused or lost connection, timeout, dropped reply."""


class ConnectError(NetworkError):
    """The connection couldn't be made, or was refused."""


class TimeoutError(NetworkError):
    """No answer within the timeout."""


class ConnectionDropped(NetworkError):
    """The connection closed part way through the reply."""


@runtime_checkable
class Transport(Protocol):
    """Sends one request and gives back the response: the only piece that touches the network.

    Promises (testing.check_transport checks them): send() follows
    redirects (up to MAX_REDIRECTS), dropping the headers that may carry a
    credential when the redirect leads to another host (see
    credential_headers); decompresses the body; and raises NetworkError
    for a failure worth retrying (no connection, a timeout, a dropped
    reply) and another TransportError for anything else; it never raises
    for an HTTP status, which the Client judges. close() releases its
    connections.
    """

    def send(self, request: Request, timeout: Timeout) -> Response:
        """Send `request`, waiting at most `timeout.connect` to connect and `timeout.read` for each read."""
        ...

    def close(self) -> None:
        """Release connections; the transport isn't used after."""
        ...


@runtime_checkable
class Auth(Protocol):
    """Signs requests, and may send requests of its own, such as renewing a token.

    auth_flow(request) is a generator, as in httpx: it yields the request
    to send (signed), receives each response, and may yield more: a token
    request first, or the request again after a 401. The Client sends each
    through its transport. TokenRefresh is the built-in one.
    """

    def auth_flow(self, request: Request) -> Generator[Request, Response, None]:
        """Yield the requests to send for `request`, receiving each response."""
        ...

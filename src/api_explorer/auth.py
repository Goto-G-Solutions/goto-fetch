"""Authentication: fixed token headers, and access tokens that renew themselves."""

import base64
import binascii
import json
import logging
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import urljoin

import httpx2

from .errors import ApiError
from .logs import log_event
from .paths import get_path

logger = logging.getLogger(__name__)


def _renewing(how: str, why: str) -> None:
    """Log, at DEBUG, that an access token is being renewed, how and why."""
    action = (
        "Refreshing the access token"
        if how == "refresh"
        else "Logging in for new tokens"
    )
    log_event(
        logger,
        logging.DEBUG,
        "renew",
        "%s: %s",
        action,
        why,
        data={"how": how, "why": why},
    )


def token_headers(
    token: str, scheme: str | None = "Token", header: str = "Authorization"
) -> dict[str, str]:
    """Return the header that carries a fixed token.

    token_headers("abc") gives {"Authorization": "Token abc"}. Pass
    scheme="Bearer" for bearer tokens, or scheme=None with a header such as
    "X-API-Key" for APIs that want the bare token in a header of their own.
    """
    return {header: f"{scheme} {token}" if scheme else token}


def jwt_expiry(token: str) -> float | None:
    """Return when a JWT expires, as a Unix time, or None if unknown.

    Reads the token's own "exp" claim without checking its signature. That
    is safe here, since the expiry is only used to refresh early; the server
    still checks every token. A token that isn't a JWT gives None.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        return float(exp)
    return None


class Login(Protocol):
    """How to get new tokens from scratch, for TokenRefresh.

    `token_url` is where the request goes (through Client, resolved like an
    endpoint). build_request(url) makes the request to send there. The reply
    must hold the access token at TokenRefresh's `access_key`, and may hold a
    refresh token and an expiry. PasswordLogin and ClientCredentials are
    built in; any object of this shape works.
    """

    @property
    def token_url(self) -> str: ...

    def build_request(self, url: str) -> httpx2.Request: ...


@dataclass(frozen=True)
class ClientCredentials:
    """The OAuth2 client credentials grant, for one service talking to another.

    POSTs grant_type=client_credentials as a form, with the client's ID and
    secret in a Basic Authorization header (credentials_in="header", which
    OAuth servers must accept) or in the form itself ("body"). `scope`, if
    given, is sent too. The secret is kept out of repr.
    """

    token_url: str
    client_id: str
    client_secret: str = field(repr=False)
    scope: str | None = None
    credentials_in: Literal["header", "body"] = "header"

    def build_request(self, url: str) -> httpx2.Request:
        """The token request, sent to `url`, which is token_url resolved."""
        form = {"grant_type": "client_credentials"}
        if self.scope:
            form["scope"] = self.scope
        headers = {}
        if self.credentials_in == "body":
            form |= {"client_id": self.client_id, "client_secret": self.client_secret}
        else:
            pair = f"{self.client_id}:{self.client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
        return httpx2.Request("POST", url, data=form, headers=headers)


@dataclass(frozen=True)
class PasswordLogin:
    """How to log in for a fresh pair of tokens: POST a username and password.

    `token_url` follows the same rules as an endpoint when TokenRefresh is
    used through Client: "/token/" under a base URL of "https://host/api" is
    "https://host/api/token/". The field names default to simplejwt's. The
    password is kept out of repr, so it doesn't end up in logs or notebook
    output.
    """

    token_url: str
    username: str
    password: str = field(repr=False)
    username_field: str = "username"
    password_field: str = "password"

    def build_request(self, url: str) -> httpx2.Request:
        """The login request, sent to `url`, which is token_url resolved."""
        body = {
            self.username_field: self.username,
            self.password_field: self.password,
        }
        return httpx2.Request("POST", url, json=body)


class TokenRefresh(httpx2.Auth):
    """Send a short-lived access token, renewing it as needed.

    Each request carries `{header}: {scheme} {access}`. The access token is
    renewed before a request when it is missing or about to expire (within
    `leeway` seconds), going by the reply's `expires_in` or, failing that,
    the token's own JWT expiry. A request that still gets a 401 renews the
    token once and is sent again. That is safe even for POST, because a 401
    means the server refused the request before acting on it.

    Renewing POSTs the refresh token to `refresh_url`, if there is a refresh
    token and a `refresh_url`. If the reply holds a new refresh token
    (rotation), it replaces the old one. Otherwise, or if refreshing fails,
    `login` gets new tokens: any Login, such as PasswordLogin or
    ClientCredentials. With no `login`, a failed refresh raises ApiError,
    and you paste new tokens.

    Safe across threads: requests sharing this object renew one at a time,
    and a request that finds the token already renewed by another uses it
    instead of renewing again, which matters when refresh tokens rotate.

    Through Client, `refresh_url` and the login's `token_url` follow the
    same rules as endpoints, because Client calls bind() with its own url().
    Used with plain httpx2, they resolve against each request's URL, so give
    absolute URLs there. Reply fields are paths (see get_path); the defaults
    read simplejwt's names and OAuth's ("access|access_token" and so on).
    Tokens live only in this object, never on disk, and are kept out of
    repr.
    """

    requires_response_body = True

    def __init__(
        self,
        refresh_url: str | None = None,
        *,
        refresh: str | None = None,
        access: str | None = None,
        login: Login | None = None,
        access_key: str = "access|access_token",
        refresh_key: str = "refresh|refresh_token",
        expires_key: str = "expires_in",
        refresh_field: str = "refresh",
        scheme: str = "Bearer",
        header: str = "Authorization",
        leeway: float = 30.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        if refresh is None and login is None:
            raise ValueError("TokenRefresh needs a refresh token, a login, or both")
        if refresh is not None and refresh_url is None:
            raise ValueError("A refresh token needs a refresh_url to send it to")
        self.refresh_url = refresh_url
        self.login = login
        self.access_key = access_key
        self.refresh_key = refresh_key
        self.expires_key = expires_key
        self.refresh_field = refresh_field
        self.scheme = scheme
        self.header = header
        self.leeway = leeway
        self.now = now
        self._access = access
        self._refresh = refresh
        self._expires_at: float | None = None
        self._resolve: Callable[[str], str] | None = None
        self._lock = threading.Lock()

    def bind(self, resolve: Callable[[str], str]) -> None:
        """Resolve refresh and login URLs with `resolve`, such as Client.url."""
        self._resolve = resolve

    def _url(self, url: str, against: str) -> str:
        return self._resolve(url) if self._resolve else urljoin(against, url)

    def __repr__(self) -> str:
        return f"TokenRefresh(refresh_url={self.refresh_url!r}, login={self.login!r})"

    def auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        """Sign `request`, renewing the access token before or after as needed."""
        seen = self._access
        if seen is None or self._expiring():
            why = "no access token yet" if seen is None else "it expires soon"
            yield from self._renew_unless_done(str(request.url), why, seen)
        used = self._access
        request.headers[self.header] = f"{self.scheme} {used}"
        response = yield request
        if response.status_code == 401:
            yield from self._renew_unless_done(
                str(request.url), "the server answered 401", used
            )
            request.headers[self.header] = f"{self.scheme} {self._access}"
            yield request

    def _expiring(self) -> bool:
        expiry = self._expires_at
        if expiry is None:
            expiry = jwt_expiry(self._access or "")
        return expiry is not None and self.now() >= expiry - self.leeway

    def _renew_unless_done(
        self, against: str, why: str, seen: str | None
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        # One renewal at a time. A request that waited here while another
        # renewed finds a new, fresh token, and uses it: renewing again
        # would spend a refresh token that rotation has already replaced.
        with self._lock:
            if (
                self._access is not None
                and self._access != seen
                and not self._expiring()
            ):
                return
            yield from self._renew(against, why)

    def _renew(
        self, against: str, why: str
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        failure = "there is no refresh token"
        if self._refresh is not None and self.refresh_url is not None:
            body = {self.refresh_field: self._refresh}
            url = self._url(self.refresh_url, against)
            _renewing("refresh", why)
            response = yield httpx2.Request("POST", url, json=body)
            if response.is_success:
                self._take(response)
                return
            failure = f"refreshing at {url} returned HTTP {response.status_code}"
        if self.login is None:
            raise ApiError(
                f"Can't renew the access token: {failure}, and there is no "
                "login to fall back on. Paste new tokens.",
                url=against,
            )
        url = self._url(self.login.token_url, against)
        _renewing("login", why if self._refresh is None else failure)
        response = yield self.login.build_request(url)
        if not response.is_success:
            raise ApiError(
                f"Logging in at {url} returned HTTP {response.status_code}: "
                f"{response.text[:300]}",
                url=str(response.url),
                status=response.status_code,
            )
        self._take(response)

    def _take(self, response: httpx2.Response) -> None:
        try:
            body = response.json()
        except ValueError as exc:
            raise ApiError(
                f"{response.url} did not return JSON", url=str(response.url)
            ) from exc
        access = get_path(body, self.access_key)
        if not isinstance(access, str):
            raise ApiError(
                f"No access token at {self.access_key!r} in the reply",
                url=str(response.url),
            )
        self._access = access
        lifetime = get_path(body, self.expires_key)
        number = isinstance(lifetime, (int, float)) and not isinstance(lifetime, bool)
        self._expires_at = self.now() + lifetime if number else None
        refresh = get_path(body, self.refresh_key)
        if isinstance(refresh, str):
            self._refresh = refresh

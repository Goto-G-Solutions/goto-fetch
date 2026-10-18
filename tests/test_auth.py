"""Authentication: token_headers, jwt_expiry, the Logins and TokenRefresh.

PasswordLogin and ClientCredentials get tokens; TokenRefresh renews them
safely across threads and resolves its URLs as the client does
(ResolvesUrls). The defences learned from other libraries' bug histories
are marked "Guard".
"""

import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from api_explorer import (
    ApiError,
    Client,
    ClientCredentials,
    PasswordLogin,
    ResolvesUrls,
    Retry,
    TokenRefresh,
    http,
    jwt_expiry,
    token_headers,
)
from api_explorer.testing import FakeApi, TokenServer, flaky, replies

LOGIN = PasswordLogin("/token/", "me", "pw")


def make_jwt(exp: float) -> str:
    """An unsigned JWT that expires at Unix time `exp`."""

    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': exp})}.sig"


@pytest.mark.parametrize(
    ("args", "headers"),
    [
        (("abc",), {"Authorization": "Token abc"}),
        (("abc", "Bearer"), {"Authorization": "Bearer abc"}),
        (("abc", None, "X-API-Key"), {"X-API-Key": "abc"}),
    ],
)
def test_token_headers_build_a_fixed_token_header(args, headers):
    assert token_headers(*args) == headers


@pytest.mark.parametrize(
    ("token", "expiry"),
    [(make_jwt(1234.0), 1234.0), ("opaque-token", None), ("a.%%%.c", None)],
)
def test_jwt_expiry_reads_the_exp_claim_or_gives_none(token, expiry):
    assert jwt_expiry(token) == expiry


def test_token_refresh_needs_a_refresh_token_or_a_login():
    with pytest.raises(ValueError, match="needs a refresh token, a login"):
        TokenRefresh("/token/refresh/")


@pytest.mark.parametrize(
    ("options", "first_calls"),
    [
        ({"login": LOGIN}, ["/api/token/"]),
        ({"refresh": "r0"}, ["/api/token/refresh/"]),
    ],
    ids=["logs-in", "refreshes"],
)
def test_the_first_request_gets_an_access_token_first(
    mock_client, options, first_calls
):
    server = TokenServer()
    auth = TokenRefresh("/token/refresh/", **options)
    assert list(mock_client(server, auth=auth).records("/items/")) == [{"id": 1}]
    assert server.paths == [*first_calls, "/api/items/"]


def test_a_401_renews_the_access_token_and_resends_once(mock_client):
    server = TokenServer()
    auth = TokenRefresh("/token/refresh/", refresh="r0", access="stale")
    assert list(mock_client(server, auth=auth).records("/items/")) == [{"id": 1}]
    assert server.paths == ["/api/items/", "/api/token/refresh/", "/api/items/"]


@pytest.mark.parametrize(
    ("now", "calls"),
    [(990.0, ["/api/token/refresh/", "/api/items/"]), (0.0, ["/api/items/"])],
    ids=["about-to-expire", "fresh"],
)
def test_a_jwt_is_renewed_only_when_it_is_about_to_expire(mock_client, now, calls):
    server = TokenServer()
    token = make_jwt(1000.0)
    server.access_tokens.add(token)
    auth = TokenRefresh("/token/refresh/", refresh="r0", access=token, now=lambda: now)
    list(mock_client(server, auth=auth).records("/items/"))
    assert server.paths == calls


def test_a_rotated_refresh_token_replaces_the_old_one(mock_client):
    server = TokenServer(rotate=True)
    client = mock_client(server, auth=TokenRefresh("/token/refresh/", refresh="r0"))
    list(client.records("/items/"))
    server.expire_access_tokens()
    # Reusing "r0" would now fail: the server accepted the rotated token.
    list(client.records("/items/"))
    refreshes = [
        b for p, b in zip(server.paths, server.bodies) if p == "/api/token/refresh/"
    ]
    assert refreshes[0] == {"refresh": "r0"}
    assert refreshes[-1] != {"refresh": "r0"}


def test_a_failed_refresh_falls_back_to_logging_in(mock_client):
    server = TokenServer()
    auth = TokenRefresh("/token/refresh/", refresh="expired", login=LOGIN)
    assert list(mock_client(server, auth=auth).records("/items/")) == [{"id": 1}]
    assert server.paths == ["/api/token/refresh/", "/api/token/", "/api/items/"]


def test_a_failed_refresh_without_a_login_names_the_url_and_asks_for_new_tokens(
    mock_client,
):
    server = TokenServer()
    auth = TokenRefresh("/token/refresh/", refresh="expired")
    with pytest.raises(ApiError, match="Paste new tokens") as raised:
        list(mock_client(server, auth=auth).records("/items/"))
    # Regression: auth errors used to leave out the URL they tried.
    assert "refreshing at http://api.test/api/token/refresh/ returned HTTP 401" in str(
        raised.value
    )
    assert raised.value.url == "http://api.test/api/token/refresh/"
    assert server.paths == ["/api/token/refresh/"]


def test_a_failed_login_names_the_url_and_its_status(mock_client):
    server = TokenServer(accept_logins=False)
    auth = TokenRefresh("/token/refresh/", login=LOGIN)
    with pytest.raises(
        ApiError, match="Logging in at http://api.test/api/token/ returned HTTP 401"
    ) as raised:
        list(mock_client(server, auth=auth).records("/items/"))
    assert raised.value.status == 401


def test_a_401_that_survives_renewal_fails_without_looping(mock_client):
    server = TokenServer(deny_all=True)
    auth = TokenRefresh("/token/refresh/", refresh="r0")
    with pytest.raises(ApiError) as raised:
        list(mock_client(server, auth=auth).records("/items/"))
    assert raised.value.status == 401
    assert server.paths == ["/api/token/refresh/", "/api/items/"] * 2


def test_a_post_refused_with_401_is_resent_with_its_body(mock_client):
    server = TokenServer()
    auth = TokenRefresh("/token/refresh/", refresh="r0", access="stale")
    response = mock_client(server, auth=auth).request(
        "POST", "/items/", json={"status": "DONE"}
    )
    assert response.json()["results"] == [{"status": "DONE"}]
    posts = [
        b
        for r, b in zip(server.calls, server.bodies)
        if r.method == "POST" and r.url.path == "/api/items/"
    ]
    assert posts == [{"status": "DONE"}, {"status": "DONE"}]


def test_token_fields_can_have_other_names(mock_client):
    server = TokenServer(access_field="access_token")
    auth = TokenRefresh("/token/refresh/", refresh="r0", access_key="access_token")
    assert list(mock_client(server, auth=auth).records("/items/")) == [{"id": 1}]


def test_auth_urls_resolve_against_the_base_url_like_endpoints(mock_client):
    # Regression: "token/" used to resolve against the request URL, giving
    # /api/items/token/.
    server = TokenServer()
    auth = TokenRefresh(
        "token/refresh", refresh="expired", login=PasswordLogin("token", "me", "pw")
    )
    list(mock_client(server, auth=auth).records("/items/"))
    assert server.paths == ["/api/token/refresh/", "/api/token/", "/api/items/"]


def test_auth_urls_follow_the_trailing_slash_setting(mock_client):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/token/pair":
            return http.Response(200, json={"access": "a1", "refresh": "r1"})
        return http.Response(200, json={"results": [], "next": None})

    auth = TokenRefresh(
        "/token/refresh", login=PasswordLogin("/token/pair", "me", "pw")
    )
    list(mock_client(handler, auth=auth, trailing_slash=False).records("/items"))
    assert seen == ["/api/token/pair", "/api/items"]


def test_secrets_stay_out_of_repr():
    login = PasswordLogin("/token/", "me", "hunter2")
    auth = TokenRefresh("/token/refresh/", refresh="r-secret", login=login)
    for text in (repr(login), repr(auth)):
        assert "hunter2" not in text
        assert "r-secret" not in text


def test_concurrent_requests_renew_a_rotating_token_only_once():
    # Regression: requests sharing one TokenRefresh each renewed at once,
    # and with rotation only the first refresh token worked: 1 of 8
    # requests succeeded. Renewal is one at a time, and a request that
    # finds the token already renewed uses it.
    server = TokenServer(rotate=True)
    answer = server._answer

    def slow_refresh(request):
        if request.url.path == "/api/token/refresh/":
            time.sleep(0.3)  # long enough for every thread to be waiting
        return answer(request)

    server._answer = slow_refresh
    client = server.client(auth=TokenRefresh("/token/refresh/", refresh="r0"))
    start = threading.Barrier(8)

    def fetch(_):
        start.wait()
        return list(client.records("/items/"))

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(fetch, range(8)))
    assert results == [[{"id": 1}]] * 8
    assert server.paths.count("/api/token/refresh/") == 1


@pytest.mark.parametrize("credentials_in", ["header", "body"])
def test_client_credentials_get_a_token_the_oauth_way(credentials_in):
    server = TokenServer()
    login = ClientCredentials(
        "/token/", "client", "secret", credentials_in=credentials_in
    )
    # No refresh_url: client credentials have no refresh step, and the
    # default access_key reads OAuth's "access_token" as well as "access".
    auth = TokenRefresh(login=login)
    assert list(server.client(auth=auth).records("/items/")) == [{"id": 1}]
    assert server.paths == ["/api/token/", "/api/items/"]


def test_client_credentials_keep_the_secret_out_of_repr():
    assert "s3cret" not in repr(ClientCredentials("/token/", "client", "s3cret"))


def test_a_refresh_token_needs_a_refresh_url():
    with pytest.raises(ValueError, match="needs a refresh_url"):
        TokenRefresh(refresh="r0")


def test_expires_in_renews_a_token_before_it_runs_out():
    clock = [1000.0]
    server = TokenServer(expires_in=60)
    auth = TokenRefresh(
        login=ClientCredentials("/token/", "client", "secret"), now=lambda: clock[0]
    )
    client = server.client(auth=auth)
    list(client.records("/items/"))
    clock[0] += 45  # within the 30s leeway of the 60s lifetime
    list(client.records("/items/"))
    # Renewed before the second fetch, so no 401 on the way.
    assert server.paths == ["/api/token/", "/api/items/", "/api/token/", "/api/items/"]


def test_a_custom_auth_with_its_own_urls_opts_in_to_endpoint_rules():
    class Signs:
        def __init__(self):
            self.resolve = None

        def bind(self, resolve):
            self.resolve = resolve

        def auth_flow(self, request):
            yield request

    auth = Signs()
    assert isinstance(auth, ResolvesUrls)
    replies(200).client(auth=auth)
    assert auth.resolve is not None
    assert auth.resolve("keys") == "http://api.test/api/keys/"


def test_a_temporary_failure_at_the_token_endpoint_is_retried():
    # Guard (meltano #1038): a 503 from the token endpoint failed the run.
    server = TokenServer()
    answer = server._answer
    failures = [http.Response(503)]

    def answer_flaky(request):
        if request.url.path == "/api/token/" and failures:
            return failures.pop()
        return answer(request)

    server._answer = answer_flaky
    auth = TokenRefresh(login=ClientCredentials("/token/", "client", "secret"))
    assert list(server.client(auth=auth).records("/items/")) == [{"id": 1}]
    assert server.paths.count("/api/token/") == 2


def test_a_refused_login_is_not_retried():
    server = TokenServer(accept_logins=False)
    auth = TokenRefresh(login=PasswordLogin("/token/", "me", "pw"))
    with pytest.raises(ApiError, match="returned HTTP 401"):
        list(server.client(auth=auth).records("/items/"))
    assert server.paths == ["/api/token/"]


def token_requests(expires_in, leeway=30.0, calls=3):
    """How many token requests `calls` requests make, given the reply's expires_in."""
    server = TokenServer()
    answer = server._answer

    def with_expiry(request):
        response = answer(request)
        if request.url.path == "/api/token/":
            return http.Response(
                200, json={**response.json(), "expires_in": expires_in}
            )
        return response

    server._answer = with_expiry
    auth = TokenRefresh(
        login=ClientCredentials("/token/", "client", "secret"), leeway=leeway
    )
    client = server.client(auth=auth)
    for _ in range(calls):
        client.request("GET", "/items/")
    return server.paths.count("/api/token/")


@pytest.mark.parametrize("expires_in", ["3600", 0, -5, "soon", None])
def test_an_expires_in_that_is_text_zero_or_odd_doesn_t_renew_every_request(expires_in):
    # Guard (authlib #530, Airbyte #824): 0 or a negative value made
    # every request fetch a new token. Digits as text count as seconds.
    assert token_requests(expires_in) == 1


def test_the_leeway_is_capped_at_half_a_short_token_s_life():
    # Guard (authlib #716): a 30s leeway on a 20s token made it look
    # expired on arrival, so every request fetched a new one.
    assert token_requests(20, leeway=30) == 1


def test_a_refresh_reply_without_a_token_falls_back_to_logging_in():
    # Regression: a 2xx refresh reply with no access token raised at once,
    # though the docstring promises a login when refreshing fails.
    def answer(request):
        if request.url.path.endswith("/refresh/"):
            return http.Response(200, json={"detail": "ok"})
        if request.url.path.endswith("/token/"):
            return http.Response(200, json={"access": "fresh"})
        assert request.headers["Authorization"] == "Bearer fresh"
        return http.Response(200, json={"ok": True})

    auth = TokenRefresh(
        "/token/refresh/", refresh="r", login=PasswordLogin("/token/", "me", "pw")
    )
    client = Client("http://api.test/api", auth=auth, transport=FakeApi(answer))
    assert client.get_json("/items/") == {"ok": True}


def test_a_token_request_that_fails_then_succeeds_doesn_t_hang_the_retry():
    # Regression: the auth flow paused mid-renewal held its lock when the
    # send raised, so the retry waited on that lock for ever.
    server = flaky(TokenServer(), http.ConnectError("refused"))
    auth = TokenRefresh("/token/refresh/", login=PasswordLogin("/token/", "me", "pw"))
    client = Client(
        "http://api.test/api", auth=auth, transport=server, retry=Retry(backoff=0)
    )
    finished = []
    worker = threading.Thread(
        target=lambda: finished.append(client.get_json("/items/")), daemon=True
    )
    worker.start()
    worker.join(timeout=5)
    assert finished == [{"results": [{"id": 1}], "next": None}]
    assert not auth._lock.locked()


def test_an_auth_that_yields_no_request_is_refused_clearly():
    class Silent:
        def auth_flow(self, request):
            return
            yield

    with pytest.raises(TypeError, match="Silent.auth_flow yielded no request"):
        replies(200).client(auth=Silent()).request("GET", "/x/")

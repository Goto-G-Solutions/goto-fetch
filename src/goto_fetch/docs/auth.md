# Auth

Four kinds of authentication are built in, and an `Auth` of your own works too.

## Choosing

| The API wants... | Use |
|------------------|-----|
| a fixed token in a header | `Client(headers=token_headers(token, scheme, header))` |
| a short-lived access token, renewed with a refresh token | `Client(auth=TokenRefresh(refresh_url, refresh=...))` |
| a login (username and password) for tokens | `TokenRefresh(login=PasswordLogin(token_url, username, password))` |
| OAuth2 client credentials (one service to another) | `TokenRefresh(login=ClientCredentials(token_url, client_id, client_secret))` |
| something else (signing, AWS, ...) | an `Auth` of your own (`auth_flow`, as in httpx), as `Client(auth=...)`; or an `httpx2.Auth` on `Httpx2Transport(http=httpx2.Client(auth=...))` |

## What it handles for you

`TokenRefresh`:

- renews the access token before it expires (by the reply's `expires_in`, or the token's own JWT expiry), and once more after a 401;
- keeps a refresh token that rotates, and renews one request at a time across threads, so a rotated refresh token is never spent twice;
- falls back to its `login` when refreshing fails, and retries a token endpoint's temporary failures through the retry policy;
- resolves its URLs the way the client resolves endpoints;
- keeps tokens in memory only, out of `repr`.

Every request it makes, renewals included, goes through the client's transport, and counts towards a rate limit.

## Examples

```python
from goto_fetch import Client, PasswordLogin, TokenRefresh, token_headers
from goto_fetch.testing import TokenServer

# a header of the API's own, with the bare token
assert token_headers("k-123", None, "X-API-Key") == {"X-API-Key": "k-123"}
assert token_headers("abc", "Bearer") == {"Authorization": "Bearer abc"}

# log in, then renew as needed
server = TokenServer()  # a simplejwt-style API: /api/token/ and /api/token/refresh/
auth = TokenRefresh("/token/refresh/", login=PasswordLogin("/token/", "me", "pw"))
client = Client(
    "http://api.test/api", auth=auth, transport=server
)  # a fake API is a transport
assert list(client.records("/items/")) == [{"id": 1}]
assert server.paths == ["/api/token/", "/api/items/"]
```

```python skip
import os
from goto_fetch import ClientCredentials

auth = TokenRefresh(
    login=ClientCredentials(
        "https://auth.example.com/oauth/token",
        "my-client-id",
        os.environ["API_CLIENT_SECRET"],
        scope="read",
    )
)
```

Reply fields are paths: the defaults read simplejwt's names and OAuth's (`"access|access_token"`, `"refresh|refresh_token"`, `"expires_in"`). Change them with `access_key=`, `refresh_key=` and `expires_key=`, and the header with `scheme=` and `header=`. `access=` starts from an access token you already hold, `leeway=` is how many seconds before expiry a token is renewed (30, and never more than half a token's life), and `refresh_field=` names the field the refresh token is posted under. `ClientCredentials(credentials_in="body")` sends the client ID and secret in the form instead of a Basic header.

Through a `Client`, the refresh and login URLs resolve like endpoints: the client calls `bind(resolve)` on an auth that has it (`ResolvesUrls`). On its own, `TokenRefresh` resolves them against each request's URL, so give absolute URLs there.

## Traps

- Keep secrets in environment variables, not in code or URLs. Generated scripts read them from `$API_TOKEN`, `$API_REFRESH_TOKEN`, `$API_ACCESS_TOKEN`, `$API_USERNAME`, `$API_PASSWORD` and `$API_CLIENT_SECRET`.
- A login of your own is any object with `token_url` and `build_request(url)`; see [build-your-own](build-your-own.md).
- A credential never leaves the API's host. The transports drop `Authorization`, `Cookie` and any header whose name says it carries a secret (`X-API-Key`, `X-Auth-Token` and the like, see `http.credential_headers`) before following a redirect to another host; a header named otherwise, such as `X-Team`, is kept, so name credential headers for what they are. And `Client` sends its headers and `auth` only to its `base_url`'s host: a next link on another host goes unsigned, with a `withheld` warning.

# Changelog

Each release lists what changed for people using the package. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). Until 1.0, a minor version (0.x) may change the public interface.

## 0.1.0

The first release.

- `Client`: one API's base URL, headers, timeouts, retries and authentication, with JSON and paginated records.
- Paging: next links in the body (`NextLink`) or the `Link` header (`HeaderLink`), cursors in the query or the body (`Cursor`), or limit and offset (`Offset`), with an optional count, and progress logged per page. `records(method="POST", json=...)` pages a POST search, with `retry=True` when it only reads. Paging styles see each page's body and response (`Fetched`) and say what to fetch next (a URL, or a `PageRequest`).
- Authentication: fixed token headers (`token_headers`), and access tokens that renew themselves (`TokenRefresh`), safely across threads, before they expire (by `expires_in` or a JWT's own expiry). New tokens come from any `Login`: `PasswordLogin`, OAuth2 `ClientCredentials`, or your own.
- Records: `get_path`, `select`, `flatten` and `explode` reshape nested JSON without a dataframe library.
- Dataframes: `to_frame` in polars, pandas or pyarrow through narwhals, evening out mixed types at every depth, and `latest_per_key`.
- Extension points: `json_decoder` (msgspec when installed), typed records with `records(model=...)`, `default_backend`, retry policies (`RetryPolicy`, with `Retry` built in), and `Client(http=...)` for an httpx2 client of your own.
- Interfaces are Protocols (`Paging`, `RetryPolicy`, `Login`, `ResolvesUrls`), checked by type checkers; `with_query` helps paging styles set query parameters.
- Building blocks for loops of your own: `paginate` (which pages by any request, such as a POST body, with loop protection by `request_key` or your own key), `read_page`, `Client.json_body` and `converter`.
- Logging: silent until switched on (`enable_logging`, `logging_to`); every line is an event with data (`record.event`, `record.data`), including why paging stopped and why tokens were renewed; `Client(name=...)` labels a client's lines, including those of your own pieces (`log_event`, `ClientFilter`).
- `FetchPlan`: describe a fetch once, run it, or write it as a script.
- Scraping (the `scrape` extra), and the explorer notebook (the `explorer` extra).
- `api_explorer.testing`: fake APIs for testing code that fetches from APIs (next link, limit/offset, cursor, Link header, a simplejwt-style token server, fixed pages and responses), `flaky` to inject failures, and `FakeApi`, which remembers every request. For pieces you build on the interfaces: `fetched` builds a page to test a paging style on its own, and `check_retry_policy` and `check_paging` check the promises every policy and style must keep.

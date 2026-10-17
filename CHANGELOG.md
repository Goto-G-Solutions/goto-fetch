# Changelog

Each release lists what changed for people using the package. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). Until 1.0, a minor version (0.x) may change the public interface.

## 0.1.0

The first release.

- `Client`: one API's base URL, headers, timeouts, retries and authentication, with JSON and paginated records.
- Paging: next links (`NextLink`) or limit and offset (`Offset`), with an optional count, and progress logged per page.
- Authentication: fixed token headers (`token_headers`), and access tokens that renew themselves (`TokenRefresh`, with an optional `PasswordLogin`).
- Records: `get_path`, `select`, `flatten` and `explode` reshape nested JSON without a dataframe library.
- Dataframes: `to_frame` in polars, pandas or pyarrow through narwhals, evening out mixed types at every depth, and `latest_per_key`.
- Extension points: `json_decoder` (msgspec when installed), typed records with `records(model=...)`, `default_backend`, and `Retry`.
- `FetchPlan`: describe a fetch once, run it, or write it as a script.
- Scraping (the `scrape` extra), and the explorer notebook (the `explorer` extra).

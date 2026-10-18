"""Pull data from paginated JSON APIs safely, keep only what changed, and store it.

The layers, each built only on the ones before it:

- errors, paths, decode, models, calls, http: the exception and redaction,
  reading values by path, parsing JSON, typed records, calls described
  once, and the HTTP vocabulary (requests, responses, Transport, Auth).
- logs, records, retry, checks: logging events, reshaping records, the
  retry policy, and response checks.
- ordering, frames, auth, urllib3_transport, httpx2_transport, limits:
  comparing a field's values, dataframes, authentication, the two
  transports, and the rate limit's budget.
- paging: the paging loop and styles.
- incremental: the trackers, Incremental and Changes.
- client, state, warehouse: the HTTP client, state stores and
  tracked_run, and the Warehouse interface with write_run.
- duckdb_warehouse, sqlite_warehouse, scrape, testing: the built-in
  warehouses, HTML scraping, and fakes and checks for your tests.
- plan: FetchPlan describes one fetch, to run, store or write as a script.

Optional packages make it faster or richer when installed: msgspec parses
JSON faster, pydantic or msgspec models type the records, polars, pandas
or pyarrow hold the frames, and DuckDB holds a warehouse.

HTML scraping lives in api_explorer.scrape and needs the "scrape" extra.
Records are kept in a Warehouse: SqliteWarehouse needs nothing more, and
DuckDBWarehouse needs the "duckdb" extra. Fake APIs and checks for
testing your own code live in api_explorer.testing.

Guides for each piece ship in api_explorer.docs: python -m api_explorer.docs
prints the index.

The package logs nothing until asked: enable_logging() switches it on.
"""

from .auth import (
    ClientCredentials,
    Login,
    PasswordLogin,
    TokenRefresh,
    jwt_expiry,
    token_headers,
)
from .checks import ResponseCheck, error_at
from .client import Client, ResolvesUrls
from .decode import Decoder, json_decoder
from .duckdb_warehouse import DuckDBWarehouse
from .errors import ApiError, Problem, redact_url
from .frames import default_backend, latest_per_key, to_frame
from .http import Auth, Request, Response, Timeout, Transport
from .httpx2_transport import Httpx2Transport
from .incremental import (
    Changes,
    Fingerprint,
    Incremental,
    ReportsDeletions,
    Tracker,
)
from .limits import RateLimit
from .logs import (
    ClientFilter,
    RedactFilter,
    disable_logging,
    enable_logging,
    logging_to,
)
from .paging import (
    Cursor,
    Fetched,
    HeaderLink,
    Keyset,
    NextLink,
    Offset,
    PageRequest,
    PaginationLoop,
    Paging,
    follow,
    paginate,
    read_page,
    with_param,
    with_query,
)
from .paths import get_path, parse_path
from .plan import FetchPlan, package_requirement
from .records import explode, flatten, parse_datetime, select
from .retry import Failure, Retry, RetryPolicy, backoff_delay, retry_after_seconds
from .sqlite_warehouse import SqliteWarehouse
from .state import SqliteState, StateStore, tracked_run
from .urllib3_transport import Urllib3Transport
from .warehouse import SqlTables, Warehouse, write_run

__all__ = [
    "ApiError",
    "Auth",
    "Changes",
    "Client",
    "ClientCredentials",
    "ClientFilter",
    "Cursor",
    "Decoder",
    "DuckDBWarehouse",
    "Failure",
    "FetchPlan",
    "Fetched",
    "Fingerprint",
    "HeaderLink",
    "Httpx2Transport",
    "Incremental",
    "Keyset",
    "Login",
    "NextLink",
    "Offset",
    "PageRequest",
    "PaginationLoop",
    "Paging",
    "PasswordLogin",
    "Problem",
    "RateLimit",
    "RedactFilter",
    "ReportsDeletions",
    "Request",
    "ResolvesUrls",
    "Response",
    "ResponseCheck",
    "Retry",
    "RetryPolicy",
    "SqlTables",
    "SqliteState",
    "SqliteWarehouse",
    "StateStore",
    "Timeout",
    "TokenRefresh",
    "Tracker",
    "Transport",
    "Urllib3Transport",
    "Warehouse",
    "backoff_delay",
    "default_backend",
    "disable_logging",
    "enable_logging",
    "error_at",
    "explode",
    "flatten",
    "follow",
    "get_path",
    "json_decoder",
    "jwt_expiry",
    "latest_per_key",
    "logging_to",
    "package_requirement",
    "paginate",
    "parse_datetime",
    "parse_path",
    "read_page",
    "redact_url",
    "retry_after_seconds",
    "select",
    "to_frame",
    "token_headers",
    "tracked_run",
    "with_param",
    "with_query",
    "write_run",
]

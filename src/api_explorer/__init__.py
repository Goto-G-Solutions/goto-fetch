"""Pull data from paginated JSON APIs, and shape it for any dataframe library.

The layers, each built only on the ones before it:

- paths, decode: reading values by path, and parsing JSON.
- models, records, retry: typed records, reshaping lists of dicts, and
  the retry policy.
- frames, paging: dataframes through narwhals, and the paging loop.
- auth, incremental, state, client: Client fetches the records, page by page,
  or only those changed since the last run, whose state a StateStore
  keeps.
- plan: FetchPlan describes one fetch, and writes it as a script.

Optional packages make it faster or richer when installed: msgspec parses
JSON faster, pydantic or msgspec models type the records, and polars,
pandas or pyarrow hold the frames.

HTML scraping lives in api_explorer.scrape and needs the "scrape" extra.
Storing records in DuckDB lives in api_explorer.warehouse and needs the
"duckdb" extra.
Fake APIs for testing your own code live in api_explorer.testing.

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
from .errors import ApiError, Problem, redact_url
from .frames import default_backend, latest_per_key, to_frame
from .incremental import Changes, Incremental, Keyset, Tracker
from .logs import (
    ClientFilter,
    RedactFilter,
    disable_logging,
    enable_logging,
    log_event,
    logging_to,
)
from .models import as_dict, converter
from .paging import (
    Cursor,
    Fetched,
    HeaderLink,
    NextLink,
    Offset,
    Page,
    PageRequest,
    Paging,
    paginate,
    read_page,
    request_key,
    with_param,
    with_query,
)
from .paths import get_path, parse_path
from .plan import FetchPlan, package_requirement
from .records import explode, flatten, parse_datetime, select
from .retry import Failure, Retry, RetryPolicy, backoff_delay, retry_after_seconds
from .state import SqliteState, StateStore, tracked_run

__all__ = [
    "ApiError",
    "Changes",
    "Client",
    "ClientCredentials",
    "ClientFilter",
    "Cursor",
    "Decoder",
    "Failure",
    "FetchPlan",
    "Fetched",
    "HeaderLink",
    "Incremental",
    "Keyset",
    "Login",
    "NextLink",
    "Offset",
    "Page",
    "PageRequest",
    "Paging",
    "PasswordLogin",
    "Problem",
    "RedactFilter",
    "ResolvesUrls",
    "ResponseCheck",
    "Retry",
    "RetryPolicy",
    "SqliteState",
    "StateStore",
    "TokenRefresh",
    "Tracker",
    "as_dict",
    "backoff_delay",
    "converter",
    "default_backend",
    "disable_logging",
    "enable_logging",
    "error_at",
    "explode",
    "flatten",
    "get_path",
    "json_decoder",
    "jwt_expiry",
    "latest_per_key",
    "log_event",
    "logging_to",
    "package_requirement",
    "paginate",
    "parse_datetime",
    "parse_path",
    "read_page",
    "redact_url",
    "request_key",
    "retry_after_seconds",
    "select",
    "to_frame",
    "token_headers",
    "tracked_run",
    "with_param",
    "with_query",
]

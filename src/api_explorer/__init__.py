"""Pull data from paginated JSON APIs, and shape it for any dataframe library.

The layers, each built only on the ones before it:

- paths, decode: reading values by path, and parsing JSON.
- models, records, retry: typed records, reshaping lists of dicts, and
  the retry policy.
- frames, paging: dataframes through narwhals, and the paging loop.
- auth, client: Client fetches the records, page by page.
- plan: FetchPlan describes one fetch, and writes it as a script.

Optional packages make it faster or richer when installed: msgspec parses
JSON faster, pydantic or msgspec models type the records, and polars,
pandas or pyarrow hold the frames.

HTML scraping lives in api_explorer.scrape and needs the "scrape" extra.
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
from .client import Client, ResolvesUrls
from .decode import Decoder, json_decoder
from .errors import ApiError, redact_url
from .frames import default_backend, latest_per_key, to_frame
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
    with_query,
)
from .paths import get_path, parse_path
from .plan import FetchPlan, package_requirement
from .records import explode, flatten, parse_datetime, select
from .retry import Failure, Retry, RetryPolicy, backoff_delay, retry_after_seconds

__all__ = [
    "ApiError",
    "Client",
    "ClientCredentials",
    "ClientFilter",
    "Cursor",
    "Decoder",
    "Failure",
    "FetchPlan",
    "Fetched",
    "HeaderLink",
    "Login",
    "NextLink",
    "Offset",
    "Page",
    "PageRequest",
    "Paging",
    "PasswordLogin",
    "RedactFilter",
    "ResolvesUrls",
    "Retry",
    "RetryPolicy",
    "TokenRefresh",
    "as_dict",
    "backoff_delay",
    "converter",
    "default_backend",
    "disable_logging",
    "enable_logging",
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
    "with_query",
]

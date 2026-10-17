"""Pull data from paginated JSON APIs, and shape it for any dataframe library.

The layers, each built only on the ones before it:

- paths: get_path reads a value out of nested JSON by a path string.
- records: select, flatten and explode reshape lists of dicts.
- frames: to_frame and latest_per_key, through narwhals.
- paging, auth, client: Client fetches the records, page by page.
- plan: FetchPlan describes one fetch, and writes it as a script.

HTML scraping lives in api_explorer.scrape and needs the "scrape" extra.
"""

from .auth import PasswordLogin, TokenRefresh, jwt_expiry, token_headers
from .client import Client, backoff_delay, retry_after_seconds
from .errors import ApiError
from .frames import latest_per_key, to_frame
from .paging import NextLink, Offset, Page, Paging, paginate
from .paths import get_path, parse_path
from .plan import FetchPlan, package_requirement
from .records import explode, flatten, parse_datetime, select

__all__ = [
    "ApiError",
    "Client",
    "FetchPlan",
    "NextLink",
    "Offset",
    "Page",
    "Paging",
    "PasswordLogin",
    "TokenRefresh",
    "backoff_delay",
    "explode",
    "flatten",
    "get_path",
    "jwt_expiry",
    "latest_per_key",
    "package_requirement",
    "paginate",
    "parse_datetime",
    "parse_path",
    "retry_after_seconds",
    "select",
    "to_frame",
    "token_headers",
]

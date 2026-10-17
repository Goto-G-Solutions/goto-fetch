"""The one exception this package raises for failed requests and bad replies."""


class ApiError(Exception):
    """A request that failed for good.

    Raised after the retries run out, or at once for a failure that retrying
    can't fix, such as a 404. `url` is the URL that failed, and `status` is
    the HTTP status code when there was a response.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status = status

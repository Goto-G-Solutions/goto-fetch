"""Fetch only what changed since the last run: a cursor field and its last value.

An Incremental remembers the highest value of one field, such as
updated_at, and asks the API next time only for records from that value
on. Two rules keep it from losing data:

- Its new state is ready only after a run has seen every record. A run
  that fails part way keeps the old state, because records may come in
  any order and the unseen ones could be older than the newest seen.
- It asks from the last value itself (>=), not after it, and drops the
  records it has already seen there. Asking after it would lose records
  that share the last value with one already loaded.

It never saves anything itself: save Incremental.state once the records
are stored, so a crash between the two can only repeat records, never
skip them.
"""

import dataclasses
import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from typing import Any, Literal

from .errors import ApiError
from .logs import log_event
from .paging import PageRequest, with_query
from .paths import MISSING, get_path
from .records import parse_datetime

logger = logging.getLogger(__name__)


def _send_form(value: Any) -> Any:
    """The default form of a value sent to the API: as written, datetimes as ISO."""
    return value.isoformat() if isinstance(value, datetime) else value


def _comparable(value: Any, field: str) -> Any:
    """A value of the cursor field in a form that compares with the others.

    Numbers compare as numbers. Text that reads as a timestamp compares as
    a time, so "...Z" and "...+00:00" agree; a timestamp without an offset
    counts as UTC. Other text compares as text.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str, datetime)):
        raise ApiError(f"{field!r} holds {value!r}, which can't be ordered")
    if isinstance(value, str):
        value = parse_datetime(value) or value
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def _greater(a: Any, b: Any, field: str) -> bool:
    try:
        return _comparable(a, field) > _comparable(b, field)
    except TypeError as exc:
        raise ApiError(
            f"{field!r} holds values that don't compare: {a!r} and {b!r}"
        ) from exc


def _same(a: Any, b: Any, field: str) -> bool:
    return not _greater(a, b, field) and not _greater(b, a, field)


class Incremental:
    """Track one field's highest value across runs, and fetch only from it on.

    Use one Incremental per run. Pass it to Client.records(incremental=...),
    or to track() for a loop of your own, then save `state` after storing
    the records, and give it back as `state=` next time.

    - `field`: path to the value that grows as records are added or
      changed, such as "updated_at" or "meta.created". Numbers and ISO
      timestamps both work.
    - `param`: the query parameter (or body key, with send_in="body") the
      API filters by, such as "updated_since" or "updated_at__gte". Best
      is one that means "from this value on". None sends nothing and
      filters here instead: every page is still fetched, but only new
      records come through.
    - `key`: path to what identifies a record, such as "id", to recognise
      the ones already seen at the last value. Without it (the default), a
      record is recognised by its whole content, which is safer: a record
      edited again within the same instant as the last value comes
      through, where a key would pass over the edit. Use a key when the
      content holds something that changes on every fetch, such as a
      fetched-at time, or to keep the state small.
    - `start`: where the first run starts, used when there is no state.
      None fetches everything.
    - `state`: what `state` held after the last run.
    - `send_as`: turns the value into what the API expects, when its
      filter takes a different form from the field (a date, not a time).
    - `missing`: what to do with a record without the field: "raise" (the
      default, since the run can't be tracked safely), "keep" or "drop".
    """

    def __init__(
        self,
        field: str,
        param: str | None = None,
        *,
        key: str | None = None,
        send_in: Literal["query", "body"] = "query",
        start: Any = None,
        state: dict[str, Any] | None = None,
        send_as: Callable[[Any], Any] = _send_form,
        missing: Literal["raise", "keep", "drop"] = "raise",
    ) -> None:
        if send_in not in ("query", "body"):
            raise ValueError(f"send_in must be 'query' or 'body', got {send_in!r}")
        if missing not in ("raise", "keep", "drop"):
            raise ValueError(
                f"missing must be 'raise', 'keep' or 'drop', got {missing!r}"
            )
        self.field = field
        self.param = param
        self.key = key
        self.send_in = send_in
        self.send_as = send_as
        self.missing = missing
        if state is not None:
            last, seen = state.get("last"), state.get("seen", [])
        else:
            last, seen = _send_form(start), []
        if last is not None:
            _comparable(last, field)  # fail now, not on the first record
        self._last: Any = last
        self._seen: frozenset[str] = frozenset(seen)
        self._state = {"last": last, "seen": sorted(self._seen)}

    @property
    def last(self) -> Any:
        """The value this run fetches from: the last run's highest, or `start`."""
        return self._last

    @property
    def state(self) -> dict[str, Any]:
        """What to save for the next run, as plain JSON values.

        It holds the highest value seen and the records seen at it. It
        moves on only when a run has seen every record (see track); until
        then, and after a failed run, it is the state this run began with.
        """
        return dict(self._state)

    def apply(self, request: PageRequest) -> PageRequest:
        """Return the first request, asking only for records from the last value on.

        Nothing changes when there is no last value or no `param`. In the
        body, `param` joins the request's JSON object.
        """
        if self._last is None or self.param is None:
            return request
        value = self.send_as(self._last)
        if self.send_in == "query":
            return dataclasses.replace(
                request, url=with_query(request.url, self.param, value)
            )
        if request.json is not None and not isinstance(request.json, dict):
            raise ApiError(
                f"Incremental can't add {self.param!r} to a body that isn't a "
                f"JSON object: {request.json!r}"
            )
        return dataclasses.replace(
            request, json={**(request.json or {}), self.param: value}
        )

    def track(self, records: Iterable[Any]) -> Iterator[Any]:
        """Yield the records not yet seen, and move `state` on once all are seen.

        A record is passed over when its value is below the last one (an
        API that ignores or loosens the filter), or equal to it and seen in
        the last run. Records may come in any order. If the loop stops
        early, by an error or a break, `state` stays as it was.
        """
        highest, at_highest = self._last, set(self._seen)
        kept = dropped = 0
        for record in records:
            value = get_path(record, self.field, MISSING)
            if value is MISSING or value is None:
                if self.missing == "raise":
                    raise ApiError(
                        f"A record has no {self.field!r}, so the run can't be "
                        f"tracked; pass missing='keep' or 'drop' to allow it: "
                        f"{json.dumps(record, default=str)[:200]}"
                    )
                if self.missing == "drop":
                    dropped += 1
                    continue
                kept += 1
                yield record
                continue
            if self._last is not None:
                if _greater(self._last, value, self.field):
                    dropped += 1
                    continue
                if _same(value, self._last, self.field) and (
                    self._identity(record) in self._seen
                ):
                    dropped += 1
                    continue
            if highest is None or _greater(value, highest, self.field):
                highest, at_highest = value, set()
            if _same(value, highest, self.field):
                at_highest.add(self._identity(record))
            kept += 1
            yield record
        self._state = {"last": _send_form(highest), "seen": sorted(at_highest)}
        log_event(
            logger,
            logging.DEBUG,
            "incremental",
            "Kept %d new records, passed over %d; next run starts from %r",
            kept,
            dropped,
            self._state["last"],
            data={"kept": kept, "dropped": dropped, "last": self._state["last"]},
        )

    def _identity(self, record: Any) -> str:
        """What recognises `record` among those seen at the last value, as text."""
        if self.key is not None:
            found = get_path(record, self.key, MISSING)
            if found is MISSING:
                raise ApiError(f"A record has no key {self.key!r}")
            return json.dumps(found, sort_keys=True, default=str)
        whole = json.dumps(record, sort_keys=True, default=str).encode()
        return hashlib.sha256(whole).hexdigest()[:32]

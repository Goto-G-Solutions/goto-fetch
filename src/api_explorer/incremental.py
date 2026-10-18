"""Fetch or pass on only what changed since the last run.

Two trackers share one shape (Tracker), so Client.records and
tracked_run take either:

- Incremental asks the API only for what changed, by a field that grows,
  such as updated_at. It saves network time.
- Changes fetches everything and passes on only new and changed records,
  by a hash of each, and lists the ones that disappeared. It works when an
  API has no such field, or can't be trusted to update it.

An Incremental remembers the highest value of one field, such as
updated_at, and asks the API next time only for records from that value
on. Two rules keep it from losing data:

- Its new state is ready only after a run has seen every record. A run
  that fails part way keeps the old state, because records may come in
  any order and the unseen ones could be older than the newest seen.
- It asks from the last value itself (>=), not after it, and drops the
  records it has already seen there. Asking after it would lose records
  that share the last value with one already loaded.

Neither saves anything itself: save a tracker's state once the records
are stored (tracked_run does both), so a crash between the two can only
repeat records, never skip them.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from .errors import ApiError
from .logs import log_event
from .paging import Fetched, NextLinkFunction, Page, PageRequest, with_param
from .paths import MISSING, get_path, parse_path
from .records import parse_datetime

logger = logging.getLogger(__name__)


@runtime_checkable
class Tracker(Protocol):
    """Something that follows a source across runs: Incremental or Changes.

    `apply` may change the first request, `track` passes on the records
    the run should deliver, and `state` is what to save, as plain JSON. A
    tracker's state moves on only when `track` has seen every record.
    """

    @property
    def state(self) -> dict[str, Any]:
        """What to save for the next run, as plain JSON values."""
        ...

    def apply(self, request: PageRequest) -> PageRequest:
        """Return the first request, changed as this tracker needs."""
        ...

    def track(self, records: Iterable[Any]) -> Iterator[Any]:
        """Yield the records this run should deliver."""
        ...


@runtime_checkable
class ReportsDeletions(Protocol):
    """A tracker that can tell which records are gone, such as Changes.

    Warehouse.write(deleted_by=...) takes one, and removes those rows.
    """

    @property
    def deleted(self) -> list[Any] | None:
        """The keys of records gone since the last run; None until a run is complete."""
        ...


Fingerprint = Callable[[Any], str]
"""Turns a record into text that changes whenever the record does (see Changes)."""


def _digest(value: Any) -> str:
    """A short, stable fingerprint of a JSON value: 128 bits of SHA-256.

    Keys are sorted first, so the same record with its keys in another
    order gives the same fingerprint. 1 and 1.0 give different ones.
    """
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:32]


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

    A trap: paging by page number or offset over data that changes during
    the run can lose records for good. When a record is edited or deleted
    between two page requests, the records after it shift one place, so
    the next page skips one; the run still ends with a newer last value,
    and later runs never ask for the skipped record. Prefer a paging style
    the server keeps steady, such as a cursor. Changes doesn't have this
    problem: its next full fetch finds the record again.
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
        self.send_in: Literal["query", "body"] = send_in
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
        return with_param(request, self.param, self.send_as(self._last), self.send_in)

    def track(self, records: Iterable[Any]) -> Iterator[Any]:
        """Yield the records not yet seen, and move `state` on once all are seen.

        A record is passed over when its value is below the last one (an
        API that ignores or loosens the filter), or equal to it and seen in
        the last run, or when this run already passed it at its highest
        value so far (pages that overlap, as Keyset's do). Records may come
        in any order. If the loop stops early, by an error or a break,
        `state` stays as it was.
        """
        # `observed` holds what this run saw at its highest value, repeats
        # and records loaded last run included. Every record at that value
        # comes back next run (the filter asks from it), so this, not an
        # ever-growing list, is what the next run needs to recognise.
        highest, observed = self._last, set()
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
            if self._last is not None and _greater(self._last, value, self.field):
                dropped += 1
                continue
            if highest is None or _greater(value, highest, self.field):
                highest, observed = value, set()
            at_last = self._last is not None and _same(value, self._last, self.field)
            at_highest = _same(value, highest, self.field)
            if at_last or at_highest:
                identity = self._identity(record)
                if at_highest:
                    if identity in observed:
                        dropped += 1  # a repeat this run: overlapping pages
                        continue
                    observed.add(identity)
                if at_last and identity in self._seen:
                    dropped += 1  # loaded by the last run
                    continue
            kept += 1
            yield record
        self._state = {"last": _send_form(highest), "seen": sorted(observed)}
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
        return _digest(record)


def _without(value: Any, path: list[str]) -> Any:
    """`value` without what `path` points to, copying only what changes.

    Segments are dict keys, or "*" for every item of a list. A path that
    leads nowhere leaves `value` as it was.
    """
    if not path:
        return value
    head, rest = path[0], path[1:]
    if head == "*" and isinstance(value, list):
        return [_without(item, rest) for item in value]
    if not isinstance(value, dict) or head not in value:
        return value
    if not rest:
        return {k: v for k, v in value.items() if k != head}
    return {**value, head: _without(value[head], rest)}


@dataclass(frozen=True)
class Keyset:
    """Page by the records' own field: each page asks from the last value of the one before.

    For an API that sorts by a field that only grows, such as updated_at
    (often with a parameter like ordering=updated_at), and filters by it
    from a value on (`param`, such as updated_since). Page numbers and
    offsets skip a record when the data changes during a run; keyset pages
    don't, because each page starts from a value, not a position.

    Pages overlap: each starts at the last value of the one before, so the
    records at that value come again. Pair Keyset with Incremental or
    Changes, which pass the repeats over; on its own it yields them again.

    - `field`: path to the sorted value in each record.
    - `param`: the filter parameter, "from this value on" (>=). One that
      means "after this value" (>) would skip records that share a value.
    - `limit`, `limit_param`: the page size it asks for, and its name.
      Keyset needs it to tell the last page from a page full of one value.
      At least 2, since each page repeats the last record of the one before.
    - `send_in`, `send_as`: where the parameters go, and the form of the
      value, as for Incremental.

    Raises ApiError when a page isn't sorted by `field`, or when more
    records share one value than fit on a page, since it can't move past
    them (raise `limit`), or when the API ignores `param`. A server that
    caps the page size below `limit` is noticed once a short page turns
    out not to be the last; until then, a short page that moves nowhere
    counts as the end, so set `limit` no higher than the server allows.
    """

    field: str
    param: str
    limit: int = 100
    limit_param: str = "limit"
    send_in: Literal["query", "body"] = "query"
    send_as: Callable[[Any], Any] = _send_form

    def __post_init__(self) -> None:
        if self.send_in not in ("query", "body"):
            raise ValueError(f"send_in must be 'query' or 'body', got {self.send_in!r}")
        if self.limit < 2:
            # Each page after the first starts with the last record of the
            # one before, so a page of one could never move on.
            raise ValueError(
                f"limit must be at least 2 for keyset paging, got {self.limit}"
            )

    def begin(
        self, first: PageRequest, read: Callable[[Fetched], Page]
    ) -> tuple[PageRequest, NextLinkFunction]:
        """Start a run: the first request gets the page size, and keeps any filter."""
        current: Any = None  # the value the last request asked from
        page_size = self.limit  # what the server really gives, if it caps
        previous = 0  # the last page's length

        def next_link(fetched: Fetched, request: PageRequest) -> PageRequest | None:
            nonlocal current, page_size, previous
            items = read(fetched).items
            if not items:
                return None
            bottom, top = self._value_range(items, request)
            if current is not None and _greater(current, bottom, self.field):
                raise ApiError(
                    f"The page starts at {self.field}={bottom!r}, before the "
                    f"{current!r} it asked from: the API ignores {self.param!r}",
                    url=request.url,
                )
            following = with_param(request, self.param, self.send_as(top), self.send_in)
            # Asking again from where this page started moves nowhere. The
            # first request may already ask from a value (an Incremental's
            # last), which `current` doesn't know, hence the second test.
            stuck = following == request
            if stuck or (current is not None and _same(top, current, self.field)):
                # Only records at the value asked from: the repeats at the
                # end, unless the page is full, when more may follow.
                if len(items) < page_size:
                    return None
                raise ApiError(
                    f"{len(items)} or more records share {self.field}={top!r}, "
                    f"more than a page holds, so Keyset can't page past them; "
                    f"raise limit",
                    url=request.url,
                )
            if current is not None and previous < page_size:
                # The last page was short, yet this one found more: the
                # server caps the page size at what it gave.
                page_size = previous
            current, previous = top, len(items)
            return following

        return with_param(first, self.limit_param, self.limit, self.send_in), next_link

    def _value_range(self, items: list[Any], request: PageRequest) -> tuple[Any, Any]:
        """The page's first and last values of `field`, checking it is sorted by it."""
        first: Any = MISSING
        last: Any = MISSING
        for item in items:
            value = get_path(item, self.field, MISSING)
            if value is MISSING or value is None:
                raise ApiError(
                    f"A record has no {self.field!r}, so Keyset can't page past it",
                    url=request.url,
                )
            if last is not MISSING and _greater(last, value, self.field):
                raise ApiError(
                    f"The page isn't sorted by {self.field!r} ({last!r} comes "
                    f"before {value!r}); Keyset needs ascending order, often "
                    f"asked for with a parameter like ordering={self.field}",
                    url=request.url,
                )
            if first is MISSING:
                first = value
            last = value
        return first, last


class Changes:
    """Pass on only the records that are new or changed since the last run.

    It keeps a fingerprint (a hash) of each record by its key, and passes
    on a record whose key is new or whose fingerprint differs. Every run
    must fetch everything, since a record can't be compared unless it is
    fetched; what it saves is the work after the fetch, and it notices
    records that disappeared (`deleted`), which a timestamp never shows.

    Use one Changes per run, like Incremental: pass it to
    Client.records(incremental=...) or to tracked_run.

    - `key`: path to what identifies a record, such as "id". Required.
    - `ignore`: paths to leave out of the fingerprint, such as
      "fetched_at" or "stats.views": values that change on every fetch
      would make every record look changed. "*" walks a list, as in
      "items[*].stock".
    - `fingerprint`: a function of your own in place of the hash, for an
      API that gives each record a version or ETag already:
      fingerprint=lambda record: str(record["version"]). Give `ignore` or
      `fingerprint`, not both.
    - `state`: what `state` held after the last run.

    Only for runs that fetch every record: with a filtered fetch (such as
    Incremental's), the records not fetched would look deleted.
    """

    def __init__(
        self,
        key: str,
        *,
        ignore: Iterable[str] = (),
        fingerprint: Fingerprint | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.key = key
        self.ignore = [parse_path(path) for path in ignore]
        if fingerprint is not None and self.ignore:
            # Two ways to set one thing: a fingerprint of your own decides
            # what counts as a change by itself.
            raise ValueError("Give ignore or fingerprint, not both")
        self.fingerprint = fingerprint
        self._hashes: dict[str, str] = dict((state or {}).get("hashes", {}))
        self._state: dict[str, Any] = {"hashes": dict(self._hashes)}
        self._deleted: list[Any] | None = None

    @property
    def state(self) -> dict[str, Any]:
        """What to save for the next run: each key's fingerprint.

        It moves on only when a run has seen every record; until then, and
        after a failed run, it is the state this run began with.
        """
        return {"hashes": dict(self._state["hashes"])}

    @property
    def deleted(self) -> list[Any] | None:
        """The keys of the last run's records that this run didn't see.

        None until the run has seen every record, since only a complete
        run can say what is gone. Exact only when each run's records are
        stored all or nothing: a failed run that stored part of its
        records doesn't move the state, so if one of those records is
        deleted at the source before the next run, no run lists it.
        Store inside one transaction (as tracked_run's block allows), or
        write everything at the end of the block.
        """
        return None if self._deleted is None else list(self._deleted)

    def apply(self, request: PageRequest) -> PageRequest:
        """Return the first request unchanged: Changes always fetches everything."""
        return request

    def track(self, records: Iterable[Any]) -> Iterator[Any]:
        """Yield new and changed records, and move `state` on once all are seen.

        A record seen twice in one run (pages that overlap) is passed on
        once, unless it changed in between. If the loop stops early, by an
        error or a break, `state` stays as it was and `deleted` stays None.
        """
        seen: dict[str, str] = {}
        new = changed = unchanged = 0
        for record in records:
            found = get_path(record, self.key, MISSING)
            if found is MISSING or found is None:
                raise ApiError(
                    f"A record has no key {self.key!r}: "
                    f"{json.dumps(record, default=str)[:200]}"
                )
            name = json.dumps(found, sort_keys=True, default=str)
            fingerprint = self._fingerprint(record)
            before = seen.get(name, self._hashes.get(name))
            seen[name] = fingerprint
            if before == fingerprint:
                unchanged += 1
                continue
            if before is None:
                new += 1
            else:
                changed += 1
            yield record
        gone = [json.loads(name) for name in self._hashes if name not in seen]
        self._state = {"hashes": seen}
        self._deleted = gone
        log_event(
            logger,
            logging.DEBUG,
            "changes",
            "%d new, %d changed, %d unchanged, %d gone",
            new,
            changed,
            unchanged,
            len(gone),
            data={
                "new": new,
                "changed": changed,
                "unchanged": unchanged,
                "deleted": len(gone),
            },
        )

    def _fingerprint(self, record: Any) -> str:
        if self.fingerprint is not None:
            made = self.fingerprint(record)
            if not isinstance(made, str):
                raise TypeError(
                    f"fingerprint must return text, got {type(made).__name__}"
                )
            return made
        for path in self.ignore:
            record = _without(record, path)
        return _digest(record)

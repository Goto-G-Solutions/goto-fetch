"""Fake servers for the tests: handlers for httpx2.MockTransport."""

import json
from collections.abc import Callable
from typing import Any

import httpx2


def replies(*responses: Any) -> tuple[Callable, list]:
    """A handler that answers with `responses` in order, and its call log.

    Each response is an httpx2.Response, or an exception to raise. The last
    one repeats once they run out.
    """
    calls: list = []

    def handler(request: Any) -> Any:
        calls.append(request)
        reply = responses[min(len(calls), len(responses)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    return handler, calls


def pages_at(pages: dict[str, Any]) -> Callable:
    """A handler that serves a JSON page for each known URL, else 404."""

    def handler(request: Any) -> Any:
        url = str(request.url)
        if url in pages:
            return httpx2.Response(200, json=pages[url])
        return httpx2.Response(404)

    return handler


def offset_api(
    total: int,
    *,
    cap: int | None = None,
    count: int | str | None = "actual",
    ignore_offset: bool = False,
) -> tuple[Callable, list]:
    """A Django Ninja style endpoint over records 0..total-1, and its call log.

    `cap` limits the page size whatever the client asks. `count` is the count
    the page reports: "actual" for the real total, a number, or None to leave
    it out. With `ignore_offset`, every page starts at 0.
    """
    calls: list[dict] = []

    def handler(request: Any) -> Any:
        query = dict(request.url.params)
        calls.append(query)
        limit = int(query["limit"])
        offset = 0 if ignore_offset else int(query["offset"])
        size = min(limit, cap) if cap else limit
        body: dict = {"items": list(range(total))[offset : offset + size]}
        if count is not None:
            body["count"] = total if count == "actual" else count
        return httpx2.Response(200, json=body)

    return handler, calls


def auth_server(
    *,
    rotate: bool = False,
    login_works: bool = True,
    deny_all: bool = False,
    access_field: str = "access",
) -> tuple[Callable, dict]:
    """A simplejwt-style API: login, refresh, and a protected endpoint.

    Login is POST /api/token/ with {"username": "me", "password": "pw"}, and
    refresh is POST /api/token/refresh/. The refresh token "r0" starts out
    valid. state["access"] holds the access tokens it accepts, and
    state["calls"] logs every request as (method, path, json body or None).
    With `rotate`, each refresh replaces the refresh token. With `deny_all`,
    the protected endpoint refuses every token.
    """
    state: dict = {"access": set(), "refresh": {"r0"}, "n": 0, "calls": []}

    def issue(prefix: str) -> str:
        state["n"] += 1
        return f"{prefix}{state['n']}"

    def handler(request: Any) -> Any:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        state["calls"].append((request.method, path, body))
        if path == "/api/token/":
            if not login_works or body != {"username": "me", "password": "pw"}:
                return httpx2.Response(401, json={"detail": "No active account"})
            access, refresh = issue("a"), issue("r")
            state["access"].add(access)
            state["refresh"].add(refresh)
            return httpx2.Response(200, json={access_field: access, "refresh": refresh})
        if path == "/api/token/refresh/":
            old = (body or {}).get("refresh")
            if old not in state["refresh"]:
                return httpx2.Response(401, json={"detail": "Token is invalid"})
            access = issue("a")
            state["access"].add(access)
            reply = {access_field: access}
            if rotate:
                state["refresh"].discard(old)
                reply["refresh"] = issue("r")
                state["refresh"].add(reply["refresh"])
            return httpx2.Response(200, json=reply)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if deny_all or token not in state["access"]:
            return httpx2.Response(401, json={"detail": "Not authenticated"})
        return httpx2.Response(200, json={"results": [body or {"id": 1}], "next": None})

    return handler, state


def paths_called(state: dict) -> list[str]:
    """The request paths an auth_server saw, in order."""
    return [path for _, path, _ in state["calls"]]

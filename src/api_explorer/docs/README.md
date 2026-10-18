# api-explorer guides

`api_explorer` pulls data from paginated JSON APIs safely, can fetch only what changed since the last run, and stores it for analysis. It is a library to embed, not a platform to adopt: a fast start for a pipeline of your own, with each piece behind a small interface, so you bring your own for the parts you care about. These guides ship with the package, so they match the version you have. Read one from a shell with `python -m api_explorer.docs <page>`.

## Start here

Most fetches are three steps, and each has a guide:

1. **Connect:** a `Client` for one API: [client](client.md), with [auth](auth.md) and [retries](retries.md).
2. **Page through it:** `client.records(...)` with a paging style: [paging](paging.md).
3. **Keep the results:** in memory, as [records and frames](records-and-frames.md), or in a warehouse, fetching only what changed: [storage](storage.md) and [incremental](incremental.md).

[fetch-plan](fetch-plan.md) describes all three as data, to run, store or write as a script; the explorer notebook builds one.

## The guides

| Page | Read it for |
|------|-------------|
| [client](client.md) | `Client`: requests, records, errors, response checks, and how requests are sent (transports) |
| [paging](paging.md) | choosing a paging style, and loops of your own |
| [auth](auth.md) | token headers, access tokens that renew, logins |
| [retries](retries.md) | what is retried, and policies of your own |
| [incremental](incremental.md) | fetching only what changed: `Incremental`, `Changes`, `Keyset`, `tracked_run` |
| [storage](storage.md) | warehouses (DuckDB, SQLite, your own) and state stores |
| [records-and-frames](records-and-frames.md) | reshaping nested JSON, and dataframes |
| [fetch-plan](fetch-plan.md) | `FetchPlan`, generated scripts, the explorer |
| [logging](logging.md) | watching a run, and the events it logs |
| [testing](testing.md) | fake APIs, and testing code that fetches |
| [scraping](scraping.md) | HTML pages with no API |
| [build-your-own](build-your-own.md) | a piece of your own on each interface, and what you get for free |

## What the package handles for you

Use the built-in pieces and these come with them, so code that uses the package doesn't need to:

- retries with backoff for network errors, dropped connections, 429 and 5xx, honouring `Retry-After`, and never repeating a POST unless told it only reads;
- knowing when paging has ended, and refusing to loop forever;
- renewing access tokens before they expire, safely across threads;
- secrets in URLs hidden from errors and log lines;
- a tracked run's place saved only after its records are stored, so a failed run can repeat records but never skip them;
- warehouse writes that are all or nothing, with columns that follow the data and no value lost.

## For coding agents

- Prefer the built-in pieces; when one doesn't fit, build your own on its interface ([build-your-own](build-your-own.md)) and run its `check_*` from `api_explorer.testing`.
- Test against the fake APIs in `api_explorer.testing`, never the network ([testing](testing.md)).
- `python -m api_explorer.docs skill` prints an Agent Skill for this package; save it as `.claude/skills/api-explorer/SKILL.md` in a project to have an agent use it.

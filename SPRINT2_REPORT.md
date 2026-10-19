# DataBridge Sprint 2: reliable SaaS API connectors

Report date: 19 October 2026 (Asia/Calcutta).

## Delivery status

Implemented an independently testable HTTP extraction layer, GitHub Issues connector, fixed-origin mock SaaS connector/transport, bounded retry/pagination utilities, typed results/errors and structured connector telemetry.

**Delivery validation: 152 tests passed; 99.13% application statement coverage.** The existing 90% gate remains unchanged. All 33 Sprint 0/1 cases passed without edits. Ruff, formatting, strict mypy, Alembic check, frontend lint/typecheck/build and actionlint passed. No commit or push was performed. Work is ready for review.

## Initial inspection and scope

Before editing I read the attached request, AGENTS.md, README.md, pyproject.toml, application/persistence code, existing tests, database fixture and CI workflow. Git status was clean. Sprint 1 implementation and PostgreSQL CI service were present. `SPRINT1_REPORT.md` was absent from the current tree; I reported that and inspected the actual implementation rather than recreating historical evidence.

I gave a short implementation plan before editing. AGENTS.md was updated to authorize extraction-only Sprint 2 work while preserving security, tests, migration discipline and resource limits.

No database ingestion, destination writes, transformations, checkpoint persistence integration, background jobs, UI features or new product API routes were added. Existing persistence code, migration history, frontend and CI workflow are unchanged.

## Files changed

| File | Change |
| --- | --- |
| `AGENTS.md` | Authorized bounded Sprint 2 extraction; retained prior quality/security rules |
| `README.md` | Connector usage/configuration examples, budgets, lifecycle, retry behavior, cursor semantics and limitations |
| `backend/.env.example` | Commented credential-injection guidance; no credential values |
| `backend/pyproject.toml` | Moved existing HTTPX requirement from development-only to runtime dependencies |
| `backend/app/logging.py` | Allowlisted connector events/fields and raw HTTP transport-log suppression; preserved request/audit behavior |
| `backend/app/connectors/__init__.py` | New extraction package |
| `backend/app/connectors/types.py` | SourceConnector Protocol, strict configuration, typed records/results, connector/error enums and sanitized exception |
| `backend/app/connectors/http.py` | Owned reusable HTTPX lifecycle, streamed reads, deadlines, retry budgets and injected time/randomness |
| `backend/app/connectors/pagination.py` | Generic bounded collection, cursor validation/encoding and timestamp handling |
| `backend/app/connectors/github.py` | GitHub issue parsing, PR filtering, optional bearer token and validated Link pagination |
| `backend/app/connectors/mock.py` | Basic-auth mock source and deterministic no-socket transport with controlled faults |
| `backend/tests/test_connectors.py` | 119 parametrized connector/security/lifecycle cases |
| `SPRINT2_REPORT.md` | This implementation and evidence report |

The existing lock already pins HTTPX 0.28.1, so it required no version change. No new library was introduced. An editable package build/install verified HTTPX is present in runtime Requires-Dist metadata.

The following paths were verified unchanged using `git diff --exit-code`: existing foundation/persistence tests and conftest, migrations, persistence implementation, frontend, `.github/workflows/ci.yml`, and `backend/requirements-dev.lock`.

## Interface and design decisions

### Contracts and extraction results

`SourceConnector[T]` exposes:

```python
async def extract(
    *, cursor: str | None = None, since: datetime | None = None
) -> ExtractionResult[T]: ...
```

The result contains an immutable tuple of typed records, `pages_fetched` and an optional opaque `next_cursor`. Inputs/results use strict Pydantic models with hidden error input values. Connector errors expose fixed ErrorCode values rather than HTTP bodies or exception strings.

Cursor state is versioned, bounded and validated. It binds source identity, original watermark, page number and page size. A changed page size or incompatible source/watermark fails before a request. Cursors are unsigned: they carry no authentication authority and cannot select hosts, paths, headers or credentials. They are not persisted to Sprint 1 checkpoints.

The shared collector stops at a whole-page boundary and returns continuation state when the page or record budget is reached. It never silently drops the rest of an over-budget page. A server returning too many records in one page fails. Raw GitHub items, including filtered PR objects, count toward request/record budgets. Non-forward traversal and pages beyond 10,000 fail.

### HTTP ownership and bounded resources

The caller opens `async with ConnectorHttp(...)`. That object owns one AsyncClient and its injected transport, reuses the client across pages/extractions, and closes it on exit. It does not construct a client per page and cannot be reopened after closure. Tests cover normal closure, failures, cancellation while reading, cancellation during retry waiting and cancellation during transport shutdown.

HTTP mechanics are independent of issue payload parsing. Collection mechanics are shared by both connectors. There is no inheritance-heavy connector framework or database service wrapper.

| Policy | Default | Hard configuration bound |
| --- | --- | --- |
| Connect/read/write/pool timeout | 5/10/5/5 seconds | Each greater than zero and at most 30 seconds |
| Entire extraction deadline | 60 seconds | At most 300 seconds |
| Connections/keepalive | 5/5 | At most 10 |
| Response body | 1 MiB per page | At most 8 MiB |
| Requested page size | 50 | 1?100 |
| Pages per extraction | 10 | 1?100 |
| Raw records per extraction | 1,000 | At most 10,000 and sufficient for one page |
| Retry attempts per page | 4 including initial request | At most 8 |
| Backoff base/cap | 0.5/10 seconds | Base at most 10; cap at most 30 |
| Positive jitter | Up to 25% | At most 100% |
| Total retry waiting across all pages | 30 seconds | At most 120 seconds |

Redirects are disabled. Environment proxies are disabled. Only fixed HTTPS origins are allowed. Content-Length is checked before reading and actual body bytes are checked during streaming. Requests advertise identity encoding; compressed responses are rejected before decoding, avoiding a decompression expansion path. JSON constants such as NaN are rejected. Link headers and credential/record/string fields are bounded.

### Retry and rate-limit rules

Only connectivity/timeouts, 429, 500/502/503/504 and clearly identified GitHub rate-limit 403 responses retry. Ordinary 403, 400/401/404, redirects, malformed JSON/records, bad content types and protocol errors do not retry.

GitHub 403 identification uses remaining=0 or a valid Retry-After header, not arbitrary response text. Valid numeric and HTTP-date Retry-After values and applicable reset timestamps are respected within the remaining budget. If the required wait exceeds the budget, the connector fails with `retry_exhausted` rather than retrying prematurely or sleeping indefinitely. Exponential backoff has bounded positive jitter and a cap. The delay budget is shared across pages; it does not reset on each fetch. A separate real asyncio deadline bounds the whole extraction.

Clock, monotonic time, sleeper and randomness are injectable. Tests use fixed clocks and fake sleepers for retry arithmetic, so they perform no rate-limit waiting. Cancellation remains cancellation, with a sanitized event and cleanup.

## GitHub Issues connector

The endpoint is fixed to `https://api.github.com/repos/{owner}/{repository}/issues`. Owner/repository identifiers are validated. Requests use all issue states, ascending updated order, optional aware `since`, and REST API version `2026-03-10`. Authentication is optional and passed only in an Authorization header.

The connector uses Link rel=next semantics, including multiple relation tokens. Before another request it checks the exact trusted API origin, repository endpoint, unchanged filter/page-size parameters, a valid next page and duplicate/cyclic relations. It then reconstructs the request from known parameters and the validated page number rather than forwarding the untrusted URL. Redirects and renamed endpoints fail safely.

PR objects are excluded by the presence of the `pull_request` key. Output contains stable numeric ID, issue number, title, open/closed state, UTC-aware created/updated/closed timestamps, label names and a validated issue URL. Both supported label representations (strings and name-bearing objects) are normalized. Body/user/unknown issue fields are discarded. Malformed records fail the batch; they are not silently skipped.

There is no snapshot guarantee. Issues can move between numbered pages while their update times change. The continuation is not a safe committed ingestion checkpoint. Sprint 3 must handle overlap, deduplication and destination/checkpoint transaction safety.

## Mock SaaS connector

The source uses the fixed `https://mock.databridge.test/v1/records` contract with Basic Authentication, numbered pages, optional since filtering, external ID, aware update time and bounded name.

The transport performs no socket calls. A real transport is prohibited for this mock origin; HTTPX MockTransport or the supplied deterministic subclass is required. Credentials are constructor-injected SecretStr values and are compared without logging headers. There are no hardcoded transport credentials.

A bounded sequence of controlled request-order faults supports 401, 429, 500, timeout and malformed JSON, after which normal pagination resumes. Wrong credentials fail without retries. Last-page and empty-source termination, malformed pages, overfull pages and continuation reuse are tested.

## Structured logging and security evidence

The existing formatter now additionally recognizes:

- `connector_started`
- `connector_page_fetched`
- `connector_retry`
- `connector_completed`
- `connector_failed`

It permits fixed connector kind, UUID correlation ID, elapsed milliseconds, page number, status category, record count and fixed error code. Repository names, full URLs, query strings, headers, tokens, Basic credentials, raw payloads and arbitrary exception messages are omitted. Malformed telemetry extras are dropped. Standalone callers should configure the centralized logger, as the README demonstrates; the FastAPI app already does so.

HTTPX/httpcore namespace propagation is disabled with NullHandlers when the owned HTTP context opens. Their default URL/debug logs are replaced by DataBridge's safe events. Request and audit formatting/handler behavior is preserved; all original logging/security tests passed.

Executable security evidence includes:

- Token present in the expected GitHub Authorization header and absent from the query.
- Unauthenticated GitHub requests contain no Authorization header.
- Basic Auth accepted with injected credentials and rejected with incorrect credentials.
- Plain credentials, encoded Basic credentials, payload markers, headers and URLs absent from captured JSON logs.
- Exception strings and formatted sanitized errors exclude injected credential/body values.
- Untrusted hosts, HTTP, userinfo, nonstandard ports, other repository paths, fragments, changed filters and duplicate parameters rejected before following a next link.
- Redirect targets never followed; cycles stop before an additional request.
- A fixed mock endpoint cannot use real networking.
- Byte, record, page, cursor and elapsed/wait limits exercised.
- Streams/owned clients close on failure and cancellation.
- No database or product HTTP endpoints were introduced by the connector package.

## Exact executed commands

PowerShell commands below are relative to the repository root unless a working directory is specified. Repeated reads and Ruff fix/format iterations are grouped. Source edits used pathlib scripts and apply_patch-style file changes.

### Inspection and environment

```powershell
Get-Content -LiteralPath 'C:\Users\gagan\.codex\attachments\4a7a5885-8de1-4ba4-b919-fc75507ef3ca\Pasted text.txt'
Get-Content AGENTS.md
Get-Content README.md
Get-Content backend/pyproject.toml
Get-Content .github/workflows/ci.yml
Get-Content SPRINT1_REPORT.md
rg --files backend/app backend/tests backend/migrations
Get-Content backend/app/logging.py
Get-Content backend/app/persistence/protocols.py
Get-Content backend/app/persistence/database.py
Get-Content backend/app/config.py
Get-Content backend/tests/conftest.py
Get-Content backend/tests/test_persistence.py
Get-Content backend/requirements-dev.lock
Get-Content backend/app/main.py
git status --short
git log -3 --oneline
git ls-files '*REPORT*'
Test-Path .venv/pg-sprint1
Test-Path .venv/postgresql-runtime/pgsql/bin/pg_ctl.exe
Test-Path .venv/actionlint/actionlint.exe
& '.venv/postgresql-runtime/pgsql/bin/pg_ctl.exe' -D '.venv/pg-sprint1' status
```

The attempted Sprint 1 report read failed because the file was absent. The isolated database was initially stopped; existing local binaries and data directory were available.

```powershell
& '.venv/postgresql-runtime/pgsql/bin/pg_ctl.exe' -D '.venv/pg-sprint1' -l '.venv/pg-sprint1/server.log' -o '-h 127.0.0.1 -p 55439' -w start
```

The isolated server started successfully. No unrelated database was modified. Network/database and Vite build operations used approved execution outside sandbox restrictions.

### Formatting, static analysis and packaging

```powershell
.venv\Scripts\python -m ruff format backend
.venv\Scripts\python -m ruff check backend --fix
.venv\Scripts\python -m mypy backend/app backend/tests backend/migrations
.venv\Scripts\python -m pip install --no-deps -e ./backend
.venv\Scripts\python -m pip check
.venv\Scripts\python -c "from importlib.metadata import metadata; print([item for item in metadata('databridge-backend').get_all('Requires-Dist', []) if item.startswith('httpx')])"
```

The editable build/install passed, pip check reported no broken requirements, and runtime metadata printed `['httpx<1,>=0.28']`. Existing runtime dependency versions and the lock were retained.

Final strict commands, working directory `backend`:

```powershell
..\.venv\Scripts\python -m ruff check .
..\.venv\Scripts\python -m ruff format --check .
..\.venv\Scripts\python -m mypy app tests migrations
```

Results: lint passed, 22 Python files already formatted, strict mypy passed for 22 files. Initial lint/type issues were corrected, not suppressed. One intermediate issue-URL case-normalization edit applied casefold to the comparison result instead of the URL string; mypy caught it and it was corrected before a test run.

### Full test executions

Working directory `backend`. All runs used the real isolated database:

```powershell
$env:DATABRIDGE_TEST_DATABASE_URL='postgresql+psycopg://databridge@127.0.0.1:55439/databridge_sprint1_test'
```

First run:

```powershell
New-Item -ItemType Directory -Force ../.venv/pytest-cache-sprint2 | Out-Null
..\.venv\Scripts\python -m pytest --basetemp=../.venv/pytest-sprint2 -o cache_dir=../.venv/pytest-cache-sprint2 --cov-report=xml:coverage.xml --tb=short
```

Result: 141 passed, one assertion failed, 98.67% coverage. The assertion searched a formatted traceback for the generic word `secret`, which also appeared in the test function's name. It was corrected to check the actual injected credential value. Separate captured log checks already passed. No application error was suppressed or test removed.

Review run after cursor compatibility and cleanup/security additions:

```powershell
New-Item -ItemType Directory -Force ../.venv/pytest-cache-sprint2-review | Out-Null
..\.venv\Scripts\python -m pytest --basetemp=../.venv/pytest-sprint2-review -o cache_dir=../.venv/pytest-cache-sprint2-review --cov-report=xml:coverage.xml --tb=short
```

Result: 149 passed, 99.12% coverage.

Delivery run after additional timestamp/link/retry boundary cases and pre-request failure telemetry:

```powershell
New-Item -ItemType Directory -Force ../.venv/pytest-cache-sprint2-delivery | Out-Null
..\.venv\Scripts\python -m pytest --basetemp=../.venv/pytest-sprint2-delivery -o cache_dir=../.venv/pytest-cache-sprint2-delivery --cov-report=xml:coverage.xml --tb=short
```

Result: **152 passed, one existing deprecation warning, 12.64 seconds; 99.13% coverage**. Count breakdown: 119 connector cases, 12 foundation cases, and 21 existing persistence/CI cases. No database test was skipped.

### Migrations

Working directory `backend`:

```powershell
$env:DATABRIDGE_DATABASE_URL='postgresql+psycopg://databridge@127.0.0.1:55439/databridge_sprint1_test'
..\.venv\Scripts\python -m alembic current
..\.venv\Scripts\python -m alembic check
```

Results: `0001 (head)` and `No new upgrade operations detected.` The unchanged integration suite also applied migrations to isolated per-test schemas and exercised downgrade with data, re-upgrade and model parity. Sprint 1 revision history was not edited.

### Frontend and workflow

Working directory `frontend`:

```powershell
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run build
```

All passed. Vite built 28 modules. No frontend file was changed.

Repository root:

```powershell
.venv/actionlint/actionlint.exe .github/workflows/ci.yml
.venv/actionlint/actionlint.exe -version
git diff --check
git diff --exit-code -- backend/tests/test_foundation.py backend/tests/test_persistence.py backend/tests/conftest.py backend/migrations backend/app/persistence frontend .github/workflows/ci.yml backend/requirements-dev.lock
git status --short --untracked-files=all
git diff --stat
```

Actionlint 1.7.12 passed with no findings. Whitespace and unchanged-path checks passed. The workflow was preserved: its existing PostgreSQL service and full pytest command discover and execute connector tests automatically. Hosted CI was not triggered because no commit/push was performed.

### Database cleanup

```powershell
& '.venv/postgresql-runtime/pgsql/bin/psql.exe' -h 127.0.0.1 -p 55439 -U databridge -d databridge_sprint1_test -X -c 'SELECT version();' -c "SELECT count(*) AS leftover_test_schemas FROM pg_namespace WHERE nspname LIKE 'test_%';"
& '.venv/postgresql-runtime/pgsql/bin/pg_ctl.exe' -D '.venv/pg-sprint1' -m fast -w stop
```

Results: PostgreSQL 17.6 on Windows, zero leftover generated test schemas, then `server stopped`. Local coverage/cache/package artifacts remain ignored. No unrelated database or service was changed.

## Measured coverage

Coverage is statement coverage of `app`, not branch coverage or production-readiness proof. Migration execution is verified through unchanged integration tests but not included in `--cov=app`.

| Module | Statements | Missed | Coverage |
| --- | ---: | ---: | ---: |
| connectors/github.py | 96 | 0 | 100% |
| connectors/http.py | 148 | 3 | 98% |
| connectors/mock.py | 83 | 0 | 100% |
| connectors/pagination.py | 104 | 5 | 95% |
| connectors/types.py | 103 | 0 | 100% |
| logging.py | 77 | 0 | 100% |
| Existing configuration/main/persistence modules | 311 | 0 | 100% |
| **Total** | **922** | **8** | **99.13%** |

The eight uncovered statements are defensive HTTP URL/loop and timestamp/collector guard paths. They were not excluded from coverage. The minimum remains 90%. Coverage XML was generated at ignored `backend/coverage.xml`.

## Remaining limitations and unverified behavior

- No live GitHub API request was executed. HTTP behavior and payload contracts were tested deterministically with MockTransport; live API/version/rate-limit behavior remains unverified.
- The updated suite has not run on hosted GitHub Linux CI. Local tests used Python 3.12.7, PostgreSQL 17.6 and Windows. Workflow validation and preserved PostgreSQL gates passed locally.
- Frontend checks used local Node 24.12.0; CI remains on Node 22. No browser or Playwright test was run.
- The existing Starlette warning deprecating HTTPX TestClient support remains visible; it was not suppressed.
- Numbered GitHub pagination plus since is not snapshot isolation. Cursors are continuations, not persisted ingestion checkpoints. Duplicate/omitted records during concurrent source changes require Sprint 3 safety design.
- Cursors are unsigned and bind configuration, not identity/authorization. Reuse them only with the same source/dataset and page size. They grant no network destination capability.
- Basic Authentication is implemented as an HTTPS source contract against a no-socket test transport, not a real external SaaS deployment.
- Large issue bodies may exceed the default byte cap even when selected metadata is small; reduce page size or configure a permitted larger cap. Compressed responses intentionally fail rather than decompress.
- Rate-limit 403 detection is conservative and header-based. A 403 with no recognized rate-limit headers is permanent; raw body messages are not inspected.
- Configure central logging for standalone use. HTTPX/httpcore raw telemetry is muted; operators must not add separate raw request/body sinks that bypass application policy.
- Returned issue titles/labels and mock names are source data, not sanitized public API representations. The connector never logs them, and no new endpoint exposes them. Future API/storage code needs its own data-access policy.
- Existing Sprint 1 deployment/security limitations remain: no authentication, tenancy, production credential store or operational ingestion/checkpoint transaction integration was added.

## Primary references consulted

- [GitHub Issues REST API](https://docs.github.com/en/rest/issues/issues)
- [GitHub Link pagination](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api)
- [GitHub REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
- [HTTPX async client and streaming lifecycle](https://www.python-httpx.org/async/)

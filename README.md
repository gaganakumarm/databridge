# DataBridge

A locally runnable data integration platform foundation. Sprint 0 provides the health API, landing screen, logging and quality checks. Sprint 1 adds transactional PostgreSQL metadata persistence. Sprint 2 adds bounded HTTP extraction for GitHub Issues and a deterministic mock SaaS source. ETL and workers are not implemented.

## Requirements and local setup

Use Python 3.12, Node 22 LTS, and PostgreSQL 17. Docker Compose is optional for local PostgreSQL.

From the repository root on PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r backend/requirements-dev.lock
Copy-Item backend/.env.example backend/.env
```

On Unix use `python3.12 -m venv .venv`, `.venv/bin/python` and `cp` respectively. On Windows where PowerShell blocks npm.ps1, use `npm.cmd`.

For local PostgreSQL, set `POSTGRES_PASSWORD` in your shell, then run:

```sh
docker compose up -d --wait postgres
```

Compose binds port 5432 to loopback and persists development data in a named volume. The password has no committed default. Use a development password, never production credentials. Set `DATABRIDGE_DATABASE_URL` in `backend/.env` using the template there, with your URI-encoded password and database `databridge`. Do not commit `.env`.

Apply migrations and start the API from `backend`:

```sh
python -m alembic upgrade head
python -m uvicorn app.main:create_app --factory --reload --no-access-log
```

Use the virtual environment's Python executable for these commands. Run from `backend` so `.env` and relative logs resolve there. Database configuration is optional for health-only startup; metadata dependencies return 503 when it is absent. `/health` is liveness, not a database readiness check. Startup never creates tables or applies migrations automatically.

In another terminal:

```sh
cd frontend
npm ci
npm run dev
```

Open the Vite URL, normally http://localhost:5173. Its development proxy forwards `/health` to http://127.0.0.1:8000. The UI makes one abortable health request per mount with a five-second timeout. Refresh to retry. A production reverse proxy must serve the same-origin `/health` route; Vite preview only serves static assets.

## Architecture

The backend is a modular monolith. `app/config.py` validates settings; `app/logging.py` owns JSON logs; `app/main.py` owns the application factory, liveness route and correlation middleware. `app/persistence` contains SQLAlchemy models, structural repository Protocols, implementations, transaction/session lifecycle and metadata services. It introduces no product HTTP endpoints.

| Table | Responsibility and integrity |
| --- | --- |
| connections | UUID identity, unique nonblank name and source/destination kind; no credentials or connector configuration |
| pipelines | Unique name, distinct source/destination foreign keys, draft/active/paused state; connection role checks in the service |
| sync_runs | Pipeline foreign key, timezone-aware start/end, valid state and nonnegative count; one running run per pipeline |
| checkpoints | One cursor per pipeline/stream, bounded cursor, positive version; composite FK ties the run to the same pipeline |
| audit_events | Allowlisted action, UUID entity/actor/request IDs and timestamp; no free-form details; database triggers reject update, delete and truncate |

Foreign keys restrict deletion of referenced metadata. Indexes support pipeline connection lookups, run history, checkpoint uniqueness/run lookups and audit entity/time lookups. Audit entity IDs intentionally have no polymorphic foreign key so history remains independent of a particular entity table. Names are case-sensitive and unique across this single-tenant foundation.

`MetadataService` consumes a `Transaction` Protocol. Repositories add/flush, get and list with bounded pagination; tracked records can be updated inside a unit of work. Repositories never commit. A `UnitOfWork` commits on successful exit, rolls back on failure and permanently closes its session in every exit path. Each service operation also has a savepoint so a caught checkpoint conflict cannot accidentally commit partial progress or an unaudited change.

Checkpoint advancement updates the running run's count, performs a compare-and-swap cursor insert/update and appends an audit event in the same transaction. Expected version 0 means create only; later writes must provide the existing version. Conflicts raise a fixed `ConflictError`. This is metadata consistency, not an exactly-once ETL guarantee.

`TransactionDependency` provides a function-scoped FastAPI dependency, committing before a response is sent and closing after errors. It propagates the server-generated request ID. Use synchronous handlers with this synchronous database layer. Services can also receive an explicitly constructed unit of work in scripts. Do not share a session across threads/tasks or manually commit inside service operations.

The application lifespan owns an optional engine and disposes it on shutdown. The pool is bounded to five retained and five overflow connections, with a ten-second acquisition timeout, five-second connect timeout and ten-second statement timeout. SQL echo is disabled and SQLAlchemy parameter rendering is hidden. Do not log raw SQLAlchemy/Psycopg exceptions: driver details can still contain sensitive database values.

## Logging and audit safety

`GET /health` returns `{"status":"ok","service":"databridge"}`. Every HTTP request receives a fresh UUID `X-Request-ID`; caller IDs are ignored. Unexpected pre-response errors return a generic 500.

Application logs include UTC timestamp, severity, fixed event, request ID, elapsed milliseconds and status. Headers, URL paths, query strings, bodies, credentials, cursors and exception text are excluded. Reconfiguration closes old handlers and disables root propagation. Local logs rotate at 1 MiB with three backups by default. Set `DATABRIDGE_LOG_TO_FILE=false` for stdout-only deployment.

Audit rows commit with metadata. JSON audit notifications are emitted only after commit and contain allowlisted actions and UUID identifiers, never names, cursors, actor IDs or arbitrary payloads. Transactions retain at most 100 audit notifications. The durable audit row is authoritative: stdout/file emission is not an atomic external delivery channel and can be lost after commit. Append-only triggers protect ordinary SQL mutations, not a privileged owner who can disable/drop them. Production needs separate migration/application roles and access controls.

Use Uvicorn's documented `--no-access-log` option to avoid its separate URL logs. Local rotating files support one worker per file; multiworker deployments should use stdout collection. File permissions and external retention are deployment concerns.

## Migrations

Run from `backend` with `DATABRIDGE_DATABASE_URL` configured:

```sh
python -m alembic upgrade head
python -m alembic current
python -m alembic check
```

Revision `0001` creates the five tables, constraints, indexes and audit immutability triggers. `python -m alembic downgrade base` drops Sprint 1 tables and data; use it only on disposable databases. Future changes need new explicit revisions; do not edit deployed history. `alembic check` checks model/schema drift but does not verify trigger bodies; integration tests exercise the triggers directly.

## Tests and quality gates

Create a separate test database on your local server:

```sh
docker compose exec postgres createdb -U databridge databridge_test
```

Set `DATABRIDGE_TEST_DATABASE_URL` in your shell to its Psycopg URL. For PowerShell:

```powershell
$env:DATABRIDGE_TEST_DATABASE_URL = 'postgresql+psycopg://databridge:YOUR_URI_ENCODED_PASSWORD@127.0.0.1:5432/databridge_test'
```

For Unix, use `export DATABRIDGE_TEST_DATABASE_URL='...'`. The test database must end in `_test`; do not point tests at development or production data. Integration tests fail clearly if it is missing. They use real PostgreSQL, create a generated schema per test through Alembic and drop only that schema afterward. The test role needs schema-creation rights. No SQLite substitutes or mocked database results are used.

From `backend`, using the virtual environment:

```sh
python -m ruff check .
python -m ruff format --check .
python -m mypy app tests migrations
python -m pytest
```

Pytest retains the 90% application coverage gate. Tests cover foundation security, repository operations, FKs/uniqueness/checks, restricted deletes, commit/rollback, stale and competing checkpoint writers, cursor/count/audit atomicity, append-only audit safety, migrations with data, metadata drift, dependency cleanup and engine disposal. On Windows with an inaccessible shared pytest temp directory, use `python -m pytest --basetemp=../.venv/pytest-local -o cache_dir=../.venv/pytest-cache-local`.

From `frontend`, run `npm run lint`, `npm run typecheck`, and `npm run build`. No frontend feature changes are part of Sprint 1 or Sprint 2.

GitHub Actions runs a PostgreSQL 17 service, applies/checks migrations, and executes the complete backend suite with the dedicated test URL. It also runs frontend checks. The workflow password is a public, disposable CI fixture only. Tests contain assertions for the database service and migration/test gates.

Dependencies are pinned in `backend/requirements-dev.lock` and `frontend/package-lock.json`. To refresh backend dependencies deliberately, install `.[dev]` in a clean Python 3.12 environment, run the checks and regenerate the lock with `python -m pip freeze --exclude databridge-backend`.

## Sprint 2: HTTP source connectors

`app/connectors` is an independent extraction layer. It does not open database sessions, ingest rows, persist checkpoints, transform data or schedule syncs. The application exposes no endpoint that invokes a connector. Existing API, persistence and migration behavior is unchanged.

`SourceConnector[T]` defines `await extract(cursor=None, since=None) -> ExtractionResult[T]`. A result contains an immutable tuple of typed records, the number of pages fetched and an optional opaque `next_cursor`. Treat a cursor as source state, not a credential or authorization token. It is a validated, unsigned encoding bound to connector/source, watermark and page size. Invalid or mismatched cursors fail before any HTTP request.

A batch stops at a page boundary when its page budget or remaining record capacity is reached. It returns a cursor instead of dropping part of a page. Raw records count toward limits, including filtered GitHub pull requests. An overfull server page is an error. Resuming with a changed page size is rejected. There is also a hard page-number ceiling of 10,000.

GitHub extraction uses the fixed `https://api.github.com/repos/{owner}/{repository}/issues` endpoint, `state=all`, updated-time ascending order and the pinned `2026-03-10` REST API version. It accepts public-repository access with an optional injected token. Identifiers are validated; renamed repositories/redirects require an explicit configuration correction. Link `rel=next` is followed only after validating the exact HTTPS API origin, repository endpoint and unchanged query parameters. The code extracts the validated page number and reconstructs the request; it never forwards a response URL or credentials to another host. Non-forward links, duplicate next relations and page traversal beyond the ceiling fail.

Pull request objects are excluded. Issue results contain numeric ID, number, title, state, aware UTC timestamps, optional close time, label names and a validated GitHub issue URL. Label names can be represented by strings or name-bearing objects. Unknown issue fields such as body/user data are discarded. Malformed records fail the batch with a fixed error code; they are not silently skipped.

**Timestamp filtering and numbered pagination are not a snapshot.** Issues can move between pages while extraction is in progress, causing repeats or omissions. The returned continuation is not a committed ingestion checkpoint. Sprint 3 must own overlap, deduplication, destination transactions and checkpoint safety. Keep the same source/configuration when resuming.

### Owned client and configuration example

The caller owns a `ConnectorHttp` async context. It creates one HTTPX client, reuses it across pages/extractions and closes it on normal exit, failures or cancellation. An injected transport is owned and closed with that client. Reuse the open context rather than creating one client per page; closed contexts cannot be reopened. Use this asynchronous layer from async code, not by blocking a FastAPI request with `asyncio.run`.

```python
import asyncio
import os
from datetime import UTC, datetime
from pydantic import SecretStr

from app.config import Settings
from app.logging import configure_logging
from app.connectors.github import GitHubIssues
from app.connectors.http import ConnectorHttp
from app.connectors.types import GitHubConfig, HttpPolicy

async def main() -> None:
    configure_logging(Settings(log_to_file=False))
    token = os.environ.get("GITHUB_TOKEN")
    config = GitHubConfig(
        owner="octocat", repository="Hello-World",
        token=SecretStr(token) if token else None,
    )
    async with ConnectorHttp(HttpPolicy(page_size=25, max_pages=2)) as http:
        source = GitHubIssues(config, http)
        batch = await source.extract(since=datetime(2026, 1, 1, tzinfo=UTC))
        # Consume batch.records in your caller; do not log source payloads.
        if batch.next_cursor is not None:
            continuation = await source.extract(cursor=batch.next_cursor)
            # No persistence or scheduling occurs here.

# Optional manual network example; not executed by tests or app startup.
# asyncio.run(main())
```

Credentials are constructor-injected `SecretStr` values. Set optional `GITHUB_TOKEN` in the caller's environment, not `backend/.env` (core Settings rejects unknown .env fields). No connector credentials are automatically loaded or stored in metadata. A caller with a trusted request ID can pass it as `request_id=UUID(...)`; standalone extraction generates its own ID.

| HTTP policy | Default / maximum |
| --- | --- |
| Connect / read / write / pool timeout | 5 / 10 / 5 / 5 seconds; each at most 30 |
| Whole extraction deadline | 60 seconds; at most 300 |
| Connections / keepalive | 5 / 5; at most 10 |
| Response body | 1 MiB per page; at most 8 MiB |
| Page size / pages per batch | 50 / 10; at most 100 / 100 |
| Raw records per batch | 1,000; at most 10,000; must cover at least one page |
| Attempts per page | 4; at most 8, including initial request |
| Backoff | 0.5-second exponential base, up to 25% positive jitter, capped at 10 seconds |
| Total retry waiting | 30 seconds shared across every page; at most 120 |

Response size is checked both against Content-Length and while streaming. Encoded/compressed responses are rejected, and requests advertise `Accept-Encoding: identity`, avoiding a decompression expansion path. Titles, labels, external IDs, names, timestamps, cursors and link headers also have explicit bounds. Large repositories with large issue bodies may require a smaller page size; exceeding the byte limit fails safely.

Only timeouts/connectivity, 429, 500/502/503/504 and GitHub 403 with clear rate-limit headers are retried. Ordinary forbidden responses and 400/401/404 are permanent. GitHub rate-limit detection uses remaining=0 or a valid Retry-After header, not raw error messages. Valid Retry-After seconds/dates and GitHub reset times are respected only within the remaining wait budget. A longer required wait produces `retry_exhausted` rather than retrying early or sleeping indefinitely. Clock, monotonic time, sleeper and randomness are injectable. Cancellation is propagated after resource cleanup.

### Deterministic mock SaaS

`MockSaaS` uses Basic Authentication with injected username/password, a fixed HTTPS test endpoint and numbered pages. It returns `MockRecord(external_id, updated_at, name)` and supports `since`. The endpoint is not externally configurable. Network access to it is rejected unless an HTTPX MockTransport is injected; no real SaaS account is needed.

```python
from datetime import UTC, datetime
from pydantic import SecretStr
from app.connectors.http import ConnectorHttp
from app.connectors.mock import MockSaaS, MockSaaSTransport
from app.connectors.types import MockConfig, MockRecord

async def extract_mock(test_username: str, test_password: str) -> None:
    credentials = MockConfig(
        username=SecretStr(test_username), password=SecretStr(test_password),
    )
    transport = MockSaaSTransport(credentials, [
        MockRecord(external_id="ticket-1", updated_at=datetime(2026, 1, 1, tzinfo=UTC), name="Sample"),
    ], faults=("429", "timeout"))
    async with ConnectorHttp(transport=transport) as http:
        batch = await MockSaaS(credentials, http).extract()
```

Faults apply in request order, then normal authenticated pagination resumes. Supported faults are 401, 429, 500, timeout and malformed JSON. Credentials are supplied by tests/callers, never hardcoded into the transport. Tests inject a fake clock/sleeper; the example uses the real bounded async sleeper.

### Connector telemetry and checks

The centralized logger emits `connector_started`, `connector_page_fetched`, `connector_retry`, `connector_completed` and `connector_failed`. It allowlists connector kind, UUID correlation ID, page number, status category, duration, record count and fixed error code. It excludes repository names, URLs, query strings, headers, credentials, exception strings and payloads. Request/audit event formats are preserved. HTTPX/httpcore namespace logs are muted when a connector context opens because their raw telemetry includes URLs. Configure the application logger before standalone use, as shown above; operators should retain this policy.

All connector tests use HTTPX MockTransport or the fixed mock source; they make no external API calls. `pytest` discovers them alongside the unchanged 33 foundation/persistence cases in the existing PostgreSQL CI job. Run the full backend commands above with a real disposable PostgreSQL database; do not skip database tests to validate connectors alone.

## Scope and limitations

No credential storage, ETL, database ingestion, background workers, authentication, authorization, tenant isolation, TLS termination or public metadata/connector APIs are implemented. Actor IDs are optional internal metadata, not verified user identities. Checkpoint cursors are stored as plain bounded strings; never put credentials or row payloads into them. Access controls and encryption need deployment/product work before handling sensitive production data.

This repository remains a development foundation. See `SPRINT2_REPORT.md` for current execution evidence and remaining verification limits.

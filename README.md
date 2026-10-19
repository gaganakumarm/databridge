# DataBridge

A locally runnable data integration platform foundation. Sprint 0 provides the health API, landing screen, logging and quality checks. Sprint 1 adds transactional PostgreSQL metadata persistence. Connectors, ETL and workers are not implemented.

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

From `frontend`, run `npm run lint`, `npm run typecheck`, and `npm run build`. No frontend feature changes are part of Sprint 1.

GitHub Actions runs a PostgreSQL 17 service, applies/checks migrations, and executes the complete backend suite with the dedicated test URL. It also runs frontend checks. The workflow password is a public, disposable CI fixture only. Tests contain assertions for the database service and migration/test gates.

Dependencies are pinned in `backend/requirements-dev.lock` and `frontend/package-lock.json`. To refresh backend dependencies deliberately, install `.[dev]` in a clean Python 3.12 environment, run the checks and regenerate the lock with `python -m pip freeze --exclude databridge-backend`.

## Scope and limitations

No connectors, credential storage, ETL, background workers, authentication, authorization, tenant isolation, TLS termination or public metadata APIs are implemented. Actor IDs are optional internal metadata, not verified user identities. Checkpoint cursors are stored as plain bounded strings; never put credentials or row payloads into them. Access controls and encryption need deployment/product work before handling sensitive production data.

This repository remains a development foundation. See `SPRINT1_REPORT.md` for execution evidence and remaining verification limits.

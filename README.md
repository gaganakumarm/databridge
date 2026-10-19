# DataBridge

Sprint 0 foundation for a free, locally runnable data integration platform. Python 3.12 / FastAPI backend and React / TypeScript / Vite frontend live in separate directories.

## Local setup

Requirements: Python 3.12 and Node 22 LTS (npm included).

From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r backend/requirements-dev.lock
Copy-Item backend/.env.example backend/.env
cd backend
..\.venv\Scripts\python -m uvicorn app.main:create_app --factory --reload --no-access-log
```

On Unix use `python3.12 -m venv .venv`, `.venv/bin/python`, `cp`, and `../.venv/bin/python` respectively. Run the backend from `backend` so `.env` and relative logs resolve there. Configuration errors fail startup with field validation errors; configuration is never logged.

In another terminal:

```sh
cd frontend
npm ci
npm run dev
```

On Windows where PowerShell blocks npm.ps1, use `npm.cmd`. Open the Vite URL (normally http://localhost:5173). Its development proxy forwards `/health` to http://127.0.0.1:8000. The UI makes one abortable health request per mount with a five-second timeout. Refresh to retry. No credentials or frontend secrets are needed. The build expects same-origin `/health`; a deployment reverse proxy must forward it to the backend. Vite preview serves only static assets.

## Architecture and logging

`backend/app/config.py` validates environment configuration; `logging.py` owns JSON formatting and handlers; `main.py` exposes the application factory, typed health response and ASGI request middleware. `GET /health` returns `{"status":"ok","service":"databridge"}`. Each HTTP response, including errors, carries a fresh UUID `X-Request-ID`; caller IDs are deliberately ignored. Unknown routes and HTTP method errors are correlated too.

Logs contain UTC timestamp, severity, fixed event, request ID, elapsed milliseconds and status. Errors contain only a fixed sanitized code/detail. Headers, URL paths, query strings, bodies and exception text are excluded by design. Logger setup closes old handlers and disables propagation. Local logs rotate at 1 MiB with three backups by default. Set `DATABRIDGE_LOG_TO_FILE=false` for stdout-only cloud use. Run Uvicorn with `--no-access-log` to avoid its separate URL access logs. Use one worker per log file; multiworker deployments should use stdout with an external collector. File permissions and retention beyond local rotation belong to deployment configuration.

This is a development foundation, not an authenticated production service. No authentication, TLS termination, rate limits, ingestion limits or database schema are implemented. Do not expose it publicly without deployment hardening. Future integrations must preserve the logging allowlist and add their own security tests. Framework/dependency startup failures are outside request logging; do not add secrets to startup commands or exception messages.

## Checks

From `backend`, using the virtual environment:

```sh
python -m ruff check .
python -m ruff format --check .
python -m mypy app tests
python -m pytest
```

Pytest enforces 90% backend coverage and validates health, configuration, correlation, JSON logs, privacy, errors, rotation, handler idempotence and required CI commands. From `frontend` run `npm run lint`, `npm run typecheck`, and `npm run build`. GitHub Actions runs both sets on pushes and pull requests. The workflow test checks YAML structure and required commands; an actual hosted run requires pushing to GitHub.

Backend dependency versions are pinned in `requirements-dev.lock`; frontend versions are pinned in `package-lock.json`. To refresh backend dependencies deliberately, install `.[dev]` into a clean Python 3.12 environment, run all checks, then regenerate the lock with `python -m pip freeze --exclude databridge-backend`. Do not commit `.env`, logs, virtual environments or build output.

## Scope

Implemented: repository foundation, local environment, health API, landing screen, validated configuration, structured logging, automated checks and CI. PostgreSQL schema, connectors, authentication, pipelines and Playwright are reserved for later sprints.

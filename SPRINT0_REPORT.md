# Sprint 0 delivery report

Implemented in an initially empty repository. No later-sprint services or schema were added.

## Added files

- `.gitignore`
- `AGENTS.md`
- `README.md`
- `SPRINT0_REPORT.md`
- `.github/workflows/ci.yml`
- `backend/.env.example`
- `backend/pyproject.toml`
- `backend/requirements-dev.lock`
- `backend/app/__init__.py`
- `backend/app/config.py`
- `backend/app/logging.py`
- `backend/app/main.py`
- `backend/tests/test_foundation.py`
- `frontend/package.json`
- `frontend/package-lock.json`
- `frontend/tsconfig.json`
- `frontend/vite.config.ts`
- `frontend/eslint.config.js`
- `frontend/index.html`
- `frontend/src/main.tsx`
- `frontend/src/style.css`

Generated, ignored local artifacts: `.venv`, frontend `node_modules` and `dist`, Python caches and coverage data. No secret files were created.

## Commands and results

Commands below were executed; backend tools used the repository virtual environment. Repeated checks are grouped.

| Command / operation | Result |
| --- | --- |
| `Get-Location`, `Get-ChildItem -Force`, `rg --files`, `git status --short --untracked-files=all` | Inspected repository; initially only `.git` existed. Final status contains only foundation files. Git emitted a harmless inaccessible global-ignore warning. |
| `python --version`, `py -0p`, `node --version`, `npm --version`, `Get-Command uv` | Python 3.12.7 and Node 24.12.0 available; Python launcher found no registrations, uv absent, npm.ps1 blocked by execution policy. Used npm.cmd. |
| `python -m venv .venv` | Passed. |
| `python -m pip install -e './backend[dev]'` | Initial sandbox network denial; retry with approved network access passed. |
| `npm.cmd install` | Initial sandbox attempt cancelled after network restriction; approved retry passed. Audit: zero vulnerabilities. |
| `python -m pip freeze --exclude databridge-backend` | Generated pinned dependency file; pip warned about the empty Git repository while inspecting editable metadata. Result contains only version pins, no paths or editable entries. |
| `python -m pip install --no-index -r backend/requirements-dev.lock` | Passed; installed versions satisfy every pin. This verified the existing environment, not a fresh Linux install. |
| `python -m pip check` | Passed, no broken requirements. |
| `python -m ruff check . --fix`, `python -m ruff format .` | Applied initial formatting. |
| `python -m ruff check .` | Final run passed. |
| `python -m ruff format --check .` | Final run passed: five Python files. |
| `python -m mypy app tests` | Early attempt preceded dependency installation; subsequent run identified settings constructor typing errors in tests. Corrected with targeted ignores for Pydantic runtime parameters. Final run passed, five files. |
| `python -m pytest` | Final run: 12 passed, 100% application statement coverage, required minimum 90%. Includes CI YAML/required-command validation. |
| `npm.cmd ci --offline --ignore-scripts` | Sandbox denied replacing dependency files. |
| `npm.cmd ci` | Approved retry passed, clean install of 172 packages; audit found zero vulnerabilities. |
| `npm.cmd run lint` | Passed after clean install, zero warnings. |
| `npm.cmd run typecheck` | Passed after clean install. |
| `npm.cmd run build` | Initial sandbox denied bundler parent-directory reads; approved retry passed. Final repeat after clean install passed, 28 modules transformed. |
| `git diff --check` | Passed for tracked diff; new files remain untracked, so Ruff and compiler checks provide the substantive file validation. |

File creation and adjustments used Python pathlib scripts; reads used Get-Content and rg. One final-hardening edit initially used a repository-relative path while inside backend and failed without changing files; it was rerun from the root successfully.

## Validation coverage

Tests cover typed health JSON, clear invalid settings errors, unique server-owned correlation IDs on success and missing routes, JSON field shape/UTC timestamps/durations, excluded sensitive headers/query/body data, sanitized exception responses/logs, arbitrary log message redaction, single messages after repeated handler setup, bounded file rotation, stdout-only configuration, CI triggers/permissions/jobs and all required check commands. No external SaaS calls are used.

## Remaining limitations and unverified checks

- GitHub-hosted CI has not run. Its YAML structure, triggers, permissions, jobs and required commands passed local automated validation; no independent actionlint/schema validator was executed.
- Local frontend checks ran on Node 24.12.0; CI is configured for Node 22. A Node 22 run and fresh Linux dependency installation remain unverified.
- Browser visual inspection and browser interaction tests were not run. Playwright is intentionally deferred.
- Installed Starlette emits a deprecation warning for HTTPX TestClient support. Tests pass using the requested HTTPX stack; no alternate HTTP client was added.
- npm reports ESLint 9 as unsupported/deprecated. The pinned lint toolchain passes and npm audit reports zero vulnerabilities; a future toolchain upgrade should update ESLint and its plugins together.
- Application logging deliberately omits diagnostic exception text and traces. This protects secrets but reduces debugging detail. The guarantee applies to the centralized application logger; deployment proxies and third-party loggers need their own policies.
- Uvicorn must use the documented `--no-access-log` option to avoid separate URL logging. Rotating files support one worker per file; use stdout for multiple workers. Disk permissions and external retention are deployment responsibilities.
- The only product API is health. Authentication, TLS, rate limiting, ingestion limits, persistence and connector security belong to later work. No production security claim is made.
- No streaming endpoints exist. Once response headers are sent, middleware cannot replace a failed response with a new HTTP 500; future streaming APIs need explicit failure semantics.

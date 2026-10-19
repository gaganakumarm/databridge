# DataBridge engineering rules

- Keep a modular monolith. Apply SOLID where it clarifies responsibility; avoid speculative interfaces and abstractions.
- Keep HTTP handlers, validated configuration and logging separate. Add domain modules only when a sprint needs them.
- Never commit secrets. Validate environment settings on startup. Never log headers, query strings, credentials, tokens, bodies or raw exception messages. Return generic internal errors.
- Use centralized JSON logging with UTC timestamp, severity, fixed event name, server-generated request ID and duration. Keep one request completion event, bounded rotating local files, stdout and no propagation to root handlers.
- Test observable behavior with deterministic local fixtures. No real SaaS calls. Run Ruff, mypy, pytest with coverage, frontend lint, typecheck and build before declaring completion.
- Keep memory and disk use bounded. Avoid blocking I/O in request handlers, polling, unnecessary network calls and premature optimization. Measure before optimizing.
- Design typed, small API responses. Use pagination and request limits when collection or ingestion APIs are introduced.
- Sprint 2 adds bounded async HTTP extraction, GitHub Issues and a deterministic mock SaaS connector. Preserve Sprint 0/1 APIs, constraints, migrations, logging and tests. No database ingestion, checkpoint persistence integration, ETL, background jobs, frontend features, auth endpoints, queues, paid services, AI, Kubernetes or microservices. Playwright belongs to a later sprint.
- Connector destinations are fixed HTTPS origins. Disable redirects and environment proxies; never log URLs, query strings, headers, credentials, bodies or raw exceptions. Inject transport, time and randomness for deterministic tests. Bound pages, records, bytes, attempts, delays and total elapsed time.
- Repositories flush but never commit. Services share one injected unit of work. Store audit records in the same transaction; never put credentials, arbitrary payloads or checkpoint cursors into audit logs. Integration tests require real PostgreSQL and must not target application databases.
- Document executed checks and limitations honestly. Keep changes scoped and dependencies minimal.

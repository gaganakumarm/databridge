"""Owned, reusable HTTPX session with bounded streamed reads and retry budgets."""

import asyncio
import json
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from uuid import UUID, uuid4

import httpx

from app.connectors.types import ConnectorError, ConnectorKind, ErrorCode, HttpPolicy
from app.logging import suppress_http_transport_logs


@dataclass
class RunContext:
    kind: ConnectorKind
    request_id: UUID
    started: float
    monotonic: Callable[[], float]
    delay_spent: float = 0.0
    category: str = "local"

    def emit(
        self,
        event: str,
        *,
        page: int = 0,
        count: int = 0,
        category: str | None = None,
        code: ErrorCode | None = None,
    ) -> None:
        logging.getLogger("databridge").log(
            logging.ERROR if event == "connector_failed" else logging.INFO,
            event,
            extra={
                "connector_kind": self.kind,
                "request_id": self.request_id,
                "duration_ms": round(max(0.0, self.monotonic() - self.started) * 1000, 3),
                "page_number": page,
                "record_count": count,
                "status_category": category or self.category,
                "connector_error": code,
            },
        )


@dataclass(frozen=True)
class JsonPage:
    payload: object
    link: str | None


def retry_after(value: str | None, now: datetime) -> float | None:
    if value is None or len(value) > 128:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        # Reject huge integer conversion/float values before arithmetic.
        return float(value) if len(value) <= 9 else math.inf
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return max(0.0, (date - now).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def reject_constant(value: str) -> object:
    raise ValueError("Invalid JSON constant")


class ConnectorHttp:
    """Caller must use async with. This object owns and closes its client/transport."""

    def __init__(
        self,
        policy: HttpPolicy | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        randomness: Callable[[], float] = random.random,
    ) -> None:
        self.policy = policy or HttpPolicy()
        if self.policy.max_records < self.policy.page_size:
            raise ConnectorError(ErrorCode.CONFIGURATION)
        self._transport = transport
        self.clock, self.monotonic = clock, monotonic
        self.sleeper, self.randomness = sleeper, randomness
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ConnectorHttp":
        if self._client is not None:
            raise ConnectorError(ErrorCode.LIFECYCLE)
        suppress_http_transport_logs()
        self._client = httpx.AsyncClient(
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
            max_redirects=0,
            timeout=httpx.Timeout(
                connect=self.policy.connect_timeout,
                read=self.policy.read_timeout,
                write=self.policy.write_timeout,
                pool=self.policy.pool_timeout,
            ),
            limits=httpx.Limits(
                max_connections=self.policy.max_connections,
                max_keepalive_connections=self.policy.max_connections,
            ),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "DataBridge/0.1",
            },
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._client is not None:
            closing = asyncio.create_task(self._client.aclose())
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                await closing
                raise

    @property
    def is_closed(self) -> bool:
        return self._client is None or self._client.is_closed

    def run(self, kind: ConnectorKind, request_id: UUID | None) -> RunContext:
        return RunContext(kind, request_id or uuid4(), self.monotonic(), self.monotonic)

    async def get_json(
        self,
        url: str,
        run: RunContext,
        page: int,
        *,
        headers: dict[str, str] | None = None,
        auth: httpx.BasicAuth | None = None,
    ) -> JsonPage:
        if self._client is None or self._client.is_closed:
            raise ConnectorError(ErrorCode.LIFECYCLE)
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL:
            raise ConnectorError(ErrorCode.CONFIGURATION) from None
        allowed = (
            parsed.scheme == "https"
            and not parsed.userinfo
            and not parsed.fragment
            and parsed.port in (None, 443)
        )
        if not allowed or parsed.host not in {"api.github.com", "mock.databridge.test"}:
            raise ConnectorError(ErrorCode.CONFIGURATION)
        if parsed.host == "mock.databridge.test" and not isinstance(
            self._transport, httpx.MockTransport
        ):
            raise ConnectorError(ErrorCode.CONFIGURATION)
        for attempt in range(1, self.policy.max_attempts + 1):
            delay: float | None = None
            category = "transport"
            try:
                async with self._client.stream("GET", url, headers=headers, auth=auth) as response:
                    status = response.status_code
                    category = f"http_{status // 100}xx"
                    run.category = category
                    hinted = retry_after(response.headers.get("retry-after"), self.clock())
                    rate_limited = (
                        run.kind == ConnectorKind.GITHUB
                        and status == 403
                        and (
                            response.headers.get("x-ratelimit-remaining") == "0"
                            or hinted is not None
                        )
                    )
                    if status in {429, 500, 502, 503, 504} or rate_limited:
                        delay = hinted
                        reset = response.headers.get("x-ratelimit-reset", "")
                        if (
                            rate_limited
                            and reset.isascii()
                            and reset.isdigit()
                            and len(reset) <= 12
                        ):
                            delay = max(delay or 0.0, float(reset) - self.clock().timestamp(), 0.0)
                    elif status != 200:
                        code = {
                            401: ErrorCode.AUTHENTICATION,
                            403: ErrorCode.FORBIDDEN,
                            404: ErrorCode.NOT_FOUND,
                        }.get(status, ErrorCode.HTTP)
                        if 300 <= status < 400:
                            code = ErrorCode.REDIRECT
                        raise ConnectorError(code)
                    else:
                        result = await self._read_json(response)
                        return result
            except (httpx.TimeoutException, httpx.NetworkError):
                category = run.category = "transport"
                delay = None
            except httpx.TransportError:
                run.category = "transport"
                raise ConnectorError(ErrorCode.TRANSPORT) from None
            if attempt == self.policy.max_attempts:
                raise ConnectorError(ErrorCode.RETRIES)
            noise = max(0.0, min(1.0, self.randomness()))
            backoff = min(
                self.policy.max_backoff,
                self.policy.base_delay * 2 ** (attempt - 1) * (1 + self.policy.jitter * noise),
            )
            wait = max(backoff, delay or 0.0)
            if wait > self.policy.max_total_delay - run.delay_spent:
                raise ConnectorError(ErrorCode.RETRIES)
            run.delay_spent += wait
            run.emit("connector_retry", page=page, category=category)
            await self.sleeper(wait)
        raise ConnectorError(ErrorCode.RETRIES)

    async def _read_json(self, response: httpx.Response) -> JsonPage:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if not (
            content_type == "application/json"
            or (content_type.startswith("application/") and content_type.endswith("+json"))
        ):
            raise ConnectorError(ErrorCode.CONTENT_TYPE)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ConnectorError(ErrorCode.ENCODING)
        length = response.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdigit() or len(length) > 12:
                raise ConnectorError(ErrorCode.PAYLOAD)
            if int(length) > self.policy.max_response_bytes:
                raise ConnectorError(ErrorCode.BYTES)
        content = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            if len(content) + len(chunk) > self.policy.max_response_bytes:
                raise ConnectorError(ErrorCode.BYTES)
            content.extend(chunk)
        try:
            payload: object = json.loads(content, parse_constant=reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise ConnectorError(ErrorCode.PAYLOAD) from None
        link = response.headers.get("link")
        if link is not None and len(link) > 8192:
            raise ConnectorError(ErrorCode.PAGINATION)
        return JsonPage(payload, link)

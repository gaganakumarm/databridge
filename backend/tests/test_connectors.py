import asyncio
import base64
import io
import json
import logging
import traceback
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.connectors.github import GitHubIssues
from app.connectors.http import ConnectorHttp, retry_after
from app.connectors.mock import Fault, MockSaaS, MockSaaSTransport
from app.connectors.pagination import Cursor, encode_cursor
from app.connectors.types import (
    ConnectorError,
    ConnectorKind,
    ExtractionResult,
    GitHubConfig,
    HttpPolicy,
    Issue,
    MockConfig,
    MockRecord,
    SourceConnector,
)
from app.logging import JsonFormatter, configure_logging

NOW = datetime(2026, 1, 1, tzinfo=UTC)
CONFIG = GitHubConfig(owner="acme", repository="widgets")
CREDS = MockConfig(username=SecretStr("user-secret"), password=SecretStr("password-secret"))
Handler = Callable[[httpx.Request], httpx.Response]


class Clock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return NOW + timedelta(seconds=self.seconds)

    def monotonic(self) -> float:
        return self.seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.seconds += seconds


def issue(number: int = 1) -> dict[str, object]:
    return {
        "id": 100 + number,
        "number": number,
        "title": "payload-secret",
        "state": "open",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "closed_at": None,
        "labels": [{"name": "bug"}],
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
        "body": "body-secret",
        "credentials": "must-never-be-logged",
    }


def next_header(request: httpx.Request, page: int, relation: str = "next") -> dict[str, str]:
    return {"link": f'<{request.url.copy_set_param("page", page)}>; rel="{relation}"'}


def github(
    handler: Handler,
    *,
    policy: HttpPolicy | None = None,
    config: GitHubConfig = CONFIG,
    clock: Clock | None = None,
    cursor: str | None = None,
    since: datetime | None = None,
) -> ExtractionResult[Issue]:
    timer = clock or Clock()

    async def run() -> ExtractionResult[Issue]:
        async with ConnectorHttp(
            policy,
            transport=httpx.MockTransport(handler),
            clock=timer.now,
            monotonic=timer.monotonic,
            sleeper=timer.sleep,
            randomness=lambda: 0.5,
        ) as http:
            connector: SourceConnector[Issue] = GitHubIssues(config, http)
            return await connector.extract(cursor=cursor, since=since)

    return asyncio.run(run())


def mock(
    records: list[MockRecord],
    *,
    policy: HttpPolicy | None = None,
    faults: tuple[Fault, ...] = (),
    credentials: MockConfig = CREDS,
    since: datetime | None = None,
) -> tuple[ExtractionResult[MockRecord], int, Clock]:
    transport = MockSaaSTransport(CREDS, records, faults=faults)
    clock = Clock()

    async def run() -> ExtractionResult[MockRecord]:
        async with ConnectorHttp(
            policy,
            transport=transport,
            clock=clock.now,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
            randomness=lambda: 0.5,
        ) as http:
            connector: SourceConnector[MockRecord] = MockSaaS(credentials, http)
            return await connector.extract(since=since)

    return asyncio.run(run()), transport.calls, clock


def mock_record(number: int) -> MockRecord:
    return MockRecord(
        external_id=f"record-{number}",
        name="name-secret",
        updated_at=NOW + timedelta(seconds=number),
    )


def test_github_contract_link_pagination_pr_filtering_and_since() -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        assert "authorization" not in request.headers
        assert request.url.params["since"] == "2026-01-01T00:00:00Z"
        assert request.url.params["state"] == "all"
        assert request.url.params["sort"] == "updated"
        if page == 1:
            return httpx.Response(
                200, json=[issue(), {"pull_request": {}}], headers=next_header(request, 2)
            )
        closed = issue(2) | {"state": "closed", "closed_at": "2026-01-01T00:00:00Z"}
        return httpx.Response(200, json=[closed], headers=next_header(request, 1, "prev"))

    result = github(handler, since=NOW)
    assert pages == [1, 2] and result.pages_fetched == 2
    assert result.next_cursor is None
    assert [record.number for record in result.records] == [1, 2]
    assert result.records[0].labels == ("bug",)
    assert result.records[0].id == 101 and result.records[0].updated_at.tzinfo == UTC
    assert result.records[1].closed_at is not None
    assert "body" not in result.records[0].model_dump()


def test_token_is_header_only_and_never_in_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer github-secret"
        assert "github-secret" not in str(request.url)
        return httpx.Response(200, json=[])

    assert (
        github(
            handler,
            config=GitHubConfig(
                owner="acme", repository="widgets", token=SecretStr("github-secret")
            ),
        ).records
        == ()
    )


@pytest.mark.parametrize("limits", [{"max_pages": 1}, {"max_records": 3}])
def test_batch_bounds_resume_without_dropping_records(limits: dict[str, int]) -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        records = [issue(1), issue(2)] if page == 1 else [issue(3)]
        return httpx.Response(
            200, json=records, headers=next_header(request, 2) if page == 1 else {}
        )

    policy = HttpPolicy(page_size=2, **limits)
    first = github(handler, policy=policy, since=NOW)
    assert pages == [1] and first.next_cursor is not None
    second = github(handler, policy=policy, cursor=first.next_cursor)
    assert pages == [1, 2]
    assert [record.number for record in first.records + second.records] == [1, 2, 3]
    assert second.next_cursor is None


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.test/repos/acme/widgets/issues",
        "http://api.github.com/repos/acme/widgets/issues",
        "https://api.github.com:444/repos/acme/widgets/issues",
        "https://user@api.github.com/repos/acme/widgets/issues",
        "https://api.github.com/repos/acme/other/issues",
        "https://api.github.com/repos/acme/widgets/issues/",
        "https://api.github.com/repos/acme/widgets/issues#fragment",
        "//api.github.com/repos/acme/widgets/issues",
        "https://api.github.com/repos/acme/widgets/issues?page=2&page=3",
        "https://api.github.com/repos/acme/widgets/issues?page=0",
        "https://api.github.com/repos/acme/widgets/issues?page=2&token=secret",
        "https://api.github.com/repos/acme/widgets/issues?page=%ZZ",
    ],
)
def test_untrusted_links_fail_without_following(target: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[issue()], headers={"link": f'<{target}>; rel="next"'})

    with pytest.raises(ConnectorError, match="invalid_pagination"):
        github(handler)
    assert calls == 1


@pytest.mark.parametrize(
    "link_case", ["cycle", "duplicate", "malformed", "oversized", "changed_filter"]
)
def test_invalid_link_semantics(link_case: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        link = next_header(request, 1 if link_case == "cycle" else 2)["link"]
        if link_case == "duplicate":
            link += ", " + link
        if link_case == "malformed":
            link = "body-secret"
        if link_case == "oversized":
            link = "a" * 8193
        if link_case == "changed_filter":
            link = link.replace("state=all", "state=open")
        return httpx.Response(200, json=[issue()], headers={"link": link})

    with pytest.raises(ConnectorError, match="invalid_pagination"):
        github(handler)


def test_multiple_link_relations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        headers = next_header(request, 2, "alternate next") if page == 1 else {}
        return httpx.Response(200, json=[], headers=headers)

    assert github(handler).pages_fetched == 2


@pytest.mark.parametrize(
    "change",
    [
        {"id": True},
        {"number": "1"},
        {"title": "x" * 1025},
        {"state": "unknown"},
        {"created_at": "not-a-time"},
        {"updated_at": "2026-01-01"},
        {"updated_at": 123},
        {"labels": [123]},
        {"labels": [{"name": "x" * 101}]},
        {"labels": [{"name": "a"}] * 101},
        {"labels": None},
        {"html_url": "https://evil.test/?token=secret"},
    ],
)
def test_malformed_issues_fail_closed(change: dict[str, object]) -> None:
    with pytest.raises(ConnectorError, match="malformed_record"):
        github(lambda _: httpx.Response(200, json=[issue() | change]))


@pytest.mark.parametrize("payload", [None, {}, [1], [{"labels": []}]])
def test_invalid_issue_payloads(payload: object) -> None:
    with pytest.raises(ConnectorError):
        github(lambda _: httpx.Response(200, json=payload))


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "http_error"),
        (401, "authentication_failed"),
        (403, "forbidden"),
        (404, "not_found"),
        (422, "http_error"),
        (501, "http_error"),
        (302, "redirect_rejected"),
    ],
)
def test_permanent_errors_and_redirects_are_not_retried(status: int, code: str) -> None:
    calls = 0
    clock = Clock()

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, content=b"token-secret", headers={"location": "https://evil.test"}
        )

    with pytest.raises(ConnectorError, match=f"^{code}$"):
        github(handler, clock=clock)
    assert calls == 1 and clock.sleeps == []


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_http_statuses_retry(status: int) -> None:
    calls = 0
    clock = Clock()

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status) if calls == 1 else httpx.Response(200, json=[])

    github(handler, clock=clock)
    assert calls == 2 and clock.sleeps == [0.5625]


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError, httpx.PoolTimeout, httpx.WriteTimeout],
)
def test_transient_network_failures_retry(error: type[httpx.TransportError]) -> None:
    calls = 0
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error("credentials-secret", request=request)
        return httpx.Response(200, json=[])

    github(handler, clock=clock)
    assert calls == 2 and clock.sleeps == [0.5625]


def test_protocol_error_is_sanitized_and_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("raw-secret", request=request)

    with pytest.raises(ConnectorError, match="^transport_error$"):
        github(handler)


@pytest.mark.parametrize(
    "header,expected",
    [
        ("3", 3.0),
        (" 3 ", 3.0),
        (format_datetime(NOW + timedelta(seconds=7), usegmt=True), 7.0),
        ("garbage-secret", 0.5625),
        ("-1", 0.5625),
    ],
)
def test_retry_after_delta_date_and_invalid_value(header: str, expected: float) -> None:
    calls = 0
    clock = Clock()

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(429, headers={"retry-after": header})
            if calls == 1
            else httpx.Response(200, json=[])
        )

    github(handler, clock=clock)
    assert clock.sleeps == [expected]


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW.timestamp()) + 4)}, 4.0),
        ({"retry-after": "2"}, 2.0),
        ({"x-ratelimit-remaining": "0"}, 0.5625),
    ],
)
def test_github_rate_limit_403(headers: dict[str, str], expected: float) -> None:
    calls = 0
    clock = Clock()

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, headers=headers) if calls == 1 else httpx.Response(200, json=[])

    github(handler, clock=clock)
    assert clock.sleeps == [expected]


@pytest.mark.parametrize("header", ["121", "99999999999999999999999999999999999"])
def test_rate_wait_over_budget_fails_without_sleep(header: str) -> None:
    clock = Clock()
    with pytest.raises(ConnectorError, match="retry_exhausted"):
        github(lambda _: httpx.Response(429, headers={"retry-after": header}), clock=clock)
    assert clock.sleeps == []


def test_exponential_backoff_cap_and_attempt_exhaustion() -> None:
    clock = Clock()
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(ConnectorError, match="retry_exhausted"):
        github(handler, clock=clock, policy=HttpPolicy(base_delay=1.0, max_backoff=2.0))
    assert calls == 4 and clock.sleeps == [1.125, 2.0, 2.0]


def test_retry_wait_budget_is_shared_across_pages() -> None:
    clock = Clock()
    calls: dict[int, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        calls[page] = calls.get(page, 0) + 1
        if calls[page] == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, json=[issue()], headers=next_header(request, 2))

    with pytest.raises(ConnectorError, match="retry_exhausted"):
        github(handler, clock=clock, policy=HttpPolicy(max_total_delay=3.0))
    assert clock.sleeps == [2.0] and calls == {1: 2, 2: 1}


@pytest.mark.parametrize(
    "response,code",
    [
        (
            httpx.Response(200, content=b"secret", headers={"content-type": "application/json"}),
            "malformed_payload",
        ),
        (
            httpx.Response(200, content=b"NaN", headers={"content-type": "application/json"}),
            "malformed_payload",
        ),
        (
            httpx.Response(200, content=b"\xff", headers={"content-type": "application/json"}),
            "malformed_payload",
        ),
        (
            httpx.Response(200, content=b"[]", headers={"content-type": "text/html"}),
            "invalid_content_type",
        ),
        (
            httpx.Response(
                200,
                stream=httpx.ByteStream(b"[]"),
                headers={"content-type": "application/json", "content-encoding": "br"},
            ),
            "unsupported_encoding",
        ),
        (
            httpx.Response(
                200,
                content=b"[]",
                headers={"content-type": "application/json", "content-length": "-1"},
            ),
            "malformed_payload",
        ),
    ],
)
def test_json_and_content_validation(response: httpx.Response, code: str) -> None:
    with pytest.raises(ConnectorError, match=code):
        github(lambda _: response)


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...], block: asyncio.Event | None = None) -> None:
        self.chunks, self.block, self.closed = chunks, block, False
        self.entered = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.entered.set()
        for chunk in self.chunks:
            yield chunk
        if self.block is not None:
            await self.block.wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("declared", [True, False])
def test_declared_and_streamed_oversize_is_bounded(declared: bool) -> None:
    stream = Stream((b"x" * 20, b"x" * 20))
    headers = {"content-type": "application/json"}
    if declared:
        headers["content-length"] = "40"
    with pytest.raises(ConnectorError, match="response_too_large"):
        github(
            lambda _: httpx.Response(200, headers=headers, stream=stream),
            policy=HttpPolicy(max_response_bytes=10),
        )
    assert stream.closed


def test_record_limit_and_page_number_ceiling() -> None:
    with pytest.raises(ConnectorError, match="record_limit"):
        github(
            lambda _: httpx.Response(200, json=[issue(), issue(2)]), policy=HttpPolicy(page_size=1)
        )
    cursor = encode_cursor(Cursor(source="github:acme/widgets", page=1, since=None), 10000)
    with pytest.raises(ConnectorError, match="invalid_pagination"):
        github(
            lambda request: httpx.Response(200, json=[], headers=next_header(request, 10001)),
            cursor=cursor,
        )


@pytest.mark.parametrize(
    "cursor", ["!", "a" * 1025, "x", base64.urlsafe_b64encode(b"[]").decode().rstrip("=")]
)
def test_bad_cursors_are_sanitized(cursor: str) -> None:
    with pytest.raises(ConnectorError, match="invalid_cursor"):
        github(lambda _: pytest.fail("must not request"), cursor=cursor)


def test_cursor_source_since_version_and_timezone_validation() -> None:
    for state in [
        Cursor(source="github:other/repo", page=1, since=None),
        Cursor(version=2, source="github:acme/widgets", page=1, since=None),
        Cursor(source="github:acme/widgets", page=1, since="secret"),
    ]:
        with pytest.raises(ConnectorError, match="invalid_cursor"):
            github(lambda _: pytest.fail("must not request"), cursor=encode_cursor(state, 2))
    cursor = encode_cursor(
        Cursor(source="github:acme/widgets", page=1, since="2025-01-01T00:00:00Z"), 2
    )
    with pytest.raises(ConnectorError, match="invalid_cursor"):
        github(lambda _: pytest.fail("must not request"), cursor=cursor, since=NOW)
    with pytest.raises(ConnectorError, match="invalid_configuration"):
        github(lambda _: pytest.fail("must not request"), since=datetime(2026, 1, 1))


def test_mock_contract_basic_auth_pagination_and_since() -> None:
    records = [mock_record(number) for number in range(1, 5)]
    result, calls, _ = mock(
        records, policy=HttpPolicy(page_size=2), since=NOW + timedelta(seconds=1)
    )
    assert calls == 2 and result.pages_fetched == 2 and result.next_cursor is None
    assert [record.external_id for record in result.records] == ["record-2", "record-3", "record-4"]
    assert mock([])[0].records == ()
    invalid = MockConfig(username=CREDS.username, password=SecretStr("wrong-secret"))
    with pytest.raises(ConnectorError, match="authentication_failed"):
        mock(records, credentials=invalid)


@pytest.mark.parametrize("fault", ["429", "500", "timeout"])
def test_mock_controlled_transient_faults(fault: Fault) -> None:
    result, calls, clock = mock([mock_record(1)], faults=(fault,))
    assert len(result.records) == 1 and calls == 2 and len(clock.sleeps) == 1


@pytest.mark.parametrize(
    "fault,code", [("401", "authentication_failed"), ("malformed", "malformed_payload")]
)
def test_mock_controlled_permanent_faults(fault: Fault, code: str) -> None:
    with pytest.raises(ConnectorError, match=code):
        mock([], faults=(fault,))


@pytest.mark.parametrize(
    "payload,code",
    [
        ([], "malformed_payload"),
        ({"records": None, "next_page": None}, "malformed_payload"),
        ({"records": [], "next_page": "2"}, "invalid_pagination"),
        ({"records": [], "next_page": 1}, "invalid_pagination"),
        ({"records": [1], "next_page": None}, "malformed_record"),
        (
            {
                "records": [{"external_id": "a", "updated_at": "secret", "name": "n"}],
                "next_page": None,
            },
            "malformed_record",
        ),
        (
            {
                "records": [{"external_id": 1, "updated_at": "2026-01-01T00:00:00Z", "name": "n"}],
                "next_page": None,
            },
            "malformed_record",
        ),
    ],
)
def test_mock_rejects_invalid_payloads(payload: object, code: str) -> None:
    async def run() -> None:
        async with ConnectorHttp(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        ) as http:
            await MockSaaS(CREDS, http).extract()

    with pytest.raises(ConnectorError, match=code):
        asyncio.run(run())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"owner": "../bad", "repository": "repo"},
        {"owner": "a", "repository": ".."},
        {"owner": "a", "repository": "repo/escape"},
        {"owner": "a", "repository": "a?token=secret"},
        {"owner": "a", "repository": "a", "token": SecretStr("secret\nheader")},
    ],
)
def test_github_configuration_is_strict(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as error:
        GitHubConfig(**kwargs)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"endpoint": "https://evil.test"},
        {"endpoint": "http://127.0.0.1"},
        {"username": SecretStr("user:secret")},
        {"password": SecretStr("")},
    ],
)
def test_mock_configuration_is_fixed_and_secret_safe(kwargs: dict[str, Any]) -> None:
    values = {"username": CREDS.username, "password": CREDS.password} | kwargs
    with pytest.raises(ValidationError) as error:
        MockConfig(**values)
    assert "secret" not in str(error.value)


def test_client_lifecycle_reuse_and_mock_cannot_use_network() -> None:
    class TrackingTransport(httpx.MockTransport):
        closes = 0
        calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            return await super().handle_async_request(request)

        async def aclose(self) -> None:
            self.closes += 1
            await super().aclose()

    transport = TrackingTransport(lambda _: httpx.Response(200, json=[]))
    http = ConnectorHttp(transport=transport)

    async def run() -> None:
        connector = GitHubIssues(CONFIG, http)
        with pytest.raises(ConnectorError, match="client_not_open"):
            await connector.extract()
        async with http:
            assert not http.is_closed
            await connector.extract()
            await connector.extract()
            with pytest.raises(ConnectorError, match="client_not_open"):
                async with http:
                    pass
        assert http.is_closed
        with pytest.raises(ConnectorError, match="client_not_open"):
            await connector.extract()
        async with ConnectorHttp() as real_http:
            with pytest.raises(ConnectorError, match="invalid_configuration"):
                await MockSaaS(CREDS, real_http).extract()
            for target in [
                "http://api.github.com",
                "https://evil.test",
                "https://user@api.github.com",
            ]:
                with pytest.raises(ConnectorError, match="invalid_configuration"):
                    await real_http.get_json(target, real_http.run(ConnectorKind.GITHUB, None), 1)

    asyncio.run(run())
    assert transport.calls == 2 and transport.closes == 1
    with pytest.raises(ConnectorError, match="invalid_configuration"):
        ConnectorHttp(HttpPolicy(page_size=2, max_records=1))


def test_cancellation_closes_stream_and_owned_transport() -> None:
    async def run() -> None:
        stream = Stream((), asyncio.Event())
        http = ConnectorHttp(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, stream=stream, headers={"content-type": "application/json"}
                )
            )
        )

        async def extract() -> None:
            async with http:
                await GitHubIssues(CONFIG, http).extract()

        task = asyncio.create_task(extract())
        await stream.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed and http.is_closed

    asyncio.run(run())


def test_cancellation_during_retry_sleep_closes_client() -> None:
    async def run() -> None:
        entered = asyncio.Event()

        async def sleep(_: float) -> None:
            entered.set()
            await asyncio.Event().wait()

        http = ConnectorHttp(
            transport=httpx.MockTransport(lambda _: httpx.Response(503)), sleeper=sleep
        )

        async def extract() -> None:
            async with http:
                await GitHubIssues(CONFIG, http).extract()

        task = asyncio.create_task(extract())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert http.is_closed

    asyncio.run(run())


def test_overall_extraction_deadline_closes_stream() -> None:
    async def run() -> None:
        stream = Stream((), asyncio.Event())
        http = ConnectorHttp(
            HttpPolicy(extraction_timeout=0.01),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, stream=stream, headers={"content-type": "application/json"}
                )
            ),
        )
        with pytest.raises(ConnectorError, match="deadline_exceeded"):
            async with http:
                await GitHubIssues(CONFIG, http).extract()
        assert http.is_closed and stream.closed

    asyncio.run(run())


def test_structured_connector_logs_and_errors_are_secret_safe() -> None:
    logger = configure_logging(Settings(log_to_file=False))
    stream = io.StringIO()
    handler = logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(stream)
    identifier = uuid4()
    calls = 0

    def handler_fn(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, content=b"raw-secret")
        return httpx.Response(200, json=[issue()])

    async def run() -> None:
        clock = Clock()
        async with ConnectorHttp(
            transport=httpx.MockTransport(handler_fn),
            clock=clock.now,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        ) as http:
            await GitHubIssues(
                GitHubConfig(owner="acme", repository="widgets", token=SecretStr("github-secret")),
                http,
                request_id=identifier,
            ).extract()
            logging.getLogger("httpx").info("https://api.github.com/?token=transport-secret")
            logging.getLogger("httpcore.connection").warning("Authorization: basic-secret")

    asyncio.run(run())
    with pytest.raises(ConnectorError, match="authentication_failed"):
        github(lambda _: httpx.Response(401, content=b"credentials-secret"))
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    events = [record["event"] for record in records]
    assert events[:4] == [
        "connector_started",
        "connector_retry",
        "connector_page_fetched",
        "connector_completed",
    ]
    assert events[-1] == "connector_failed"
    assert records[2]["record_count"] == 1
    assert records[3]["record_count"] == 1
    for record in records:
        assert record["connector"] == "github_issues"
        assert record["duration_ms"] >= 0
        assert {"page_number", "record_count", "status_category"} <= record.keys()
    assert all(record["request_id"] == str(identifier) for record in records[:4])
    assert "secret" not in stream.getvalue() and "https://" not in stream.getvalue()
    assert "authorization" not in stream.getvalue().lower()
    try:
        github(
            lambda _: httpx.Response(
                200, content=b"credentials-secret", headers={"content-type": "application/json"}
            )
        )
    except ConnectorError as error:
        assert "credentials-secret" not in "".join(traceback.format_exception(error))
        assert str(error) == error.code.value
    record = logging.LogRecord("databridge", logging.INFO, "", 0, "connector_failed", (), None)
    for key in [
        "request_id",
        "duration_ms",
        "connector_kind",
        "connector_error",
        "status_category",
        "page_number",
        "record_count",
        "authorization",
    ]:
        setattr(record, key, "secret")
    assert "secret" not in JsonFormatter().format(record)


def test_retry_after_parser_boundaries() -> None:
    assert retry_after(None, NOW) is None
    assert retry_after("x" * 129, NOW) is None
    assert retry_after("Thu, 01 Jan 2026 00:00:00", NOW) is None
    assert retry_after(format_datetime(NOW - timedelta(seconds=1), usegmt=True), NOW) == 0.0


def test_resume_rejects_changed_page_size() -> None:
    cursor = encode_cursor(Cursor(source="github:acme/widgets", page=1, since=None, page_size=2), 2)
    with pytest.raises(ConnectorError, match="invalid_cursor"):
        github(lambda _: pytest.fail("must not request"), cursor=cursor)


def test_mock_continuation_and_basic_auth_logs() -> None:
    logger = configure_logging(Settings(log_to_file=False))
    stream = io.StringIO()
    log_handler = logger.handlers[0]
    assert isinstance(log_handler, logging.StreamHandler)
    log_handler.setStream(stream)
    records = [mock_record(number) for number in range(1, 4)]
    transport = MockSaaSTransport(CREDS, records)

    async def run() -> None:
        async with ConnectorHttp(HttpPolicy(page_size=2, max_pages=1), transport=transport) as http:
            connector = MockSaaS(CREDS, http)
            first = await connector.extract()
            assert first.next_cursor is not None
            second = await connector.extract(cursor=first.next_cursor)
            assert len(first.records + second.records) == 3
            assert second.next_cursor is None

    asyncio.run(run())
    output = stream.getvalue()
    assert "secret" not in output and "https://" not in output
    encoded = base64.b64encode(b"user-secret:password-secret").decode()
    assert encoded not in output and "authorization" not in output.lower()
    assert all(json.loads(line)["connector"] == "mock_saas" for line in output.splitlines())
    assert "password-secret" not in repr(CREDS)


def test_explicit_timeouts_reach_httpx_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"] == {
            "connect": 1.0,
            "read": 2.0,
            "write": 3.0,
            "pool": 4.0,
        }
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["x-github-api-version"] == "2026-03-10"
        return httpx.Response(200, json=[])

    github(
        handler,
        policy=HttpPolicy(
            connect_timeout=1.0, read_timeout=2.0, write_timeout=3.0, pool_timeout=4.0
        ),
    )


def test_client_close_finishes_even_if_exit_is_cancelled() -> None:
    async def run() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowClose(httpx.MockTransport):
            closed = False

            async def aclose(self) -> None:
                entered.set()
                await release.wait()
                self.closed = True

        transport = SlowClose(lambda _: httpx.Response(200, json=[]))

        async def use() -> None:
            async with ConnectorHttp(transport=transport) as http:
                await GitHubIssues(CONFIG, http).extract()

        task = asyncio.create_task(use())
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.closed

    asyncio.run(run())


def test_mock_overfull_page_is_rejected() -> None:
    async def run() -> None:
        payload = {"records": [mock_record(1).model_dump(mode="json")] * 2, "next_page": None}
        async with ConnectorHttp(
            HttpPolicy(page_size=1),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        ) as http:
            with pytest.raises(ConnectorError, match="record_limit"):
                await MockSaaS(CREDS, http).extract()

    asyncio.run(run())


def test_deterministic_mock_transport_rejects_invalid_requests() -> None:
    transport = MockSaaSTransport(CREDS, [])

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=transport,
            auth=httpx.BasicAuth(
                CREDS.username.get_secret_value(), CREDS.password.get_secret_value()
            ),
        ) as client:
            assert (await client.get("https://mock.databridge.test/elsewhere")).status_code == 404
            assert (
                await client.get(CREDS.endpoint, params={"page": 0, "per_page": 1})
            ).status_code == 400
            assert (await client.get(CREDS.endpoint)).status_code == 400
        with pytest.raises(ConnectorError, match="invalid_configuration"):
            MockSaaSTransport(CREDS, [], faults=("500",) * 101)

    asyncio.run(run())


def test_closed_issue_url_allows_canonical_repository_case() -> None:
    record = issue() | {"html_url": "https://github.com/Acme/Widgets/issues/1"}
    assert github(lambda _: httpx.Response(200, json=[record])).records[0].id == 101


def test_label_names_and_label_objects_are_supported() -> None:
    record = issue() | {"labels": ["bug", {"name": "feature"}]}
    assert github(lambda _: httpx.Response(200, json=[record])).records[0].labels == (
        "bug",
        "feature",
    )


def test_later_page_cycle_stops_before_another_request() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        calls.append(page)
        return httpx.Response(
            200, json=[issue(page)], headers=next_header(request, 2 if page == 1 else 1)
        )

    with pytest.raises(ConnectorError, match="invalid_pagination"):
        github(handler)
    assert calls == [1, 2]

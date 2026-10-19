"""Fixed-origin mock contract and deterministic, credential-injected test server."""

import base64
import hmac
from collections.abc import Sequence
from datetime import datetime
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

import httpx
from pydantic import ValidationError

from app.connectors.http import ConnectorHttp
from app.connectors.pagination import MAX_PAGE_NUMBER, Page, collect, prepare_cursor, timestamp
from app.connectors.types import (
    ConnectorError,
    ConnectorKind,
    ErrorCode,
    ExtractionResult,
    MockConfig,
    MockRecord,
)

Fault = Literal["401", "429", "500", "timeout", "malformed"]


class MockSaaS:
    def __init__(
        self, config: MockConfig, http: ConnectorHttp, *, request_id: UUID | None = None
    ) -> None:
        self.config, self.http, self.request_id = config, http, request_id

    async def extract(
        self, *, cursor: str | None = None, since: datetime | None = None
    ) -> ExtractionResult[MockRecord]:
        run = self.http.run(ConnectorKind.MOCK, self.request_id)
        state = prepare_cursor(run, "mock:records", cursor, since, self.http.policy.page_size)

        async def fetch(page: int) -> Page[MockRecord]:
            parameters = {"page": str(page), "per_page": str(self.http.policy.page_size)}
            if state.since is not None:
                parameters["since"] = state.since
            response = await self.http.get_json(
                self.config.endpoint + "?" + urlencode(parameters),
                run,
                page,
                auth=httpx.BasicAuth(
                    self.config.username.get_secret_value(), self.config.password.get_secret_value()
                ),
            )
            payload = response.payload
            if not isinstance(payload, dict) or set(payload) != {"records", "next_page"}:
                raise ConnectorError(ErrorCode.PAYLOAD)
            raw_records, following = payload["records"], payload["next_page"]
            if not isinstance(raw_records, list):
                raise ConnectorError(ErrorCode.PAYLOAD)
            if len(raw_records) > self.http.policy.page_size:
                raise ConnectorError(ErrorCode.RECORD_LIMIT)
            if following is not None and (
                type(following) is not int or not 1 <= following <= MAX_PAGE_NUMBER
            ):
                raise ConnectorError(ErrorCode.PAGINATION)
            records: list[MockRecord] = []
            try:
                for value in raw_records:
                    if not isinstance(value, dict) or set(value) != {
                        "external_id",
                        "updated_at",
                        "name",
                    }:
                        raise ValueError
                    records.append(
                        MockRecord(
                            external_id=value["external_id"],
                            name=value["name"],
                            updated_at=timestamp(value["updated_at"]),
                        )
                    )
            except (ValueError, ValidationError):
                raise ConnectorError(ErrorCode.RECORD) from None
            run.emit("connector_page_fetched", page=page, count=len(records), category="http_2xx")
            return Page(tuple(records), len(raw_records), following)

        return await collect(self.http, run, state, fetch)


class MockSaaSTransport(httpx.MockTransport):
    """No sockets. Faults apply by request ordinal, then normal pagination resumes."""

    def __init__(
        self,
        credentials: MockConfig,
        records: Sequence[MockRecord],
        *,
        faults: Sequence[Fault] = (),
    ) -> None:
        if len(records) > 10_000 or len(faults) > 100:
            raise ConnectorError(ErrorCode.CONFIGURATION)
        self._credentials = credentials
        self._records = tuple(records)
        self._faults = tuple(faults)
        self.calls = 0
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if str(request.url.copy_with(query=None)) != self._credentials.endpoint:
            return httpx.Response(404)
        raw = (
            self._credentials.username.get_secret_value()
            + ":"
            + self._credentials.password.get_secret_value()
        )
        expected = "Basic " + base64.b64encode(raw.encode()).decode()
        if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
            return httpx.Response(401)
        if self.calls <= len(self._faults):
            fault = self._faults[self.calls - 1]
            if fault == "timeout":
                raise httpx.ReadTimeout("test-only transport failure", request=request)
            if fault == "malformed":
                return httpx.Response(
                    200, headers={"content-type": "application/json"}, content=b"{"
                )
            return httpx.Response(
                int(fault), headers={"retry-after": "0"} if fault == "429" else {}
            )
        try:
            page, size = int(request.url.params["page"]), int(request.url.params["per_page"])
            if not 1 <= page <= MAX_PAGE_NUMBER or not 1 <= size <= 100:
                raise ValueError
            records = self._records
            since = request.url.params.get("since")
            if since is not None:
                cutoff = timestamp(since)
                records = tuple(record for record in records if record.updated_at > cutoff)
        except (KeyError, ValueError, ConnectorError):
            return httpx.Response(400)
        start = (page - 1) * size
        selected = records[start : start + size]
        return httpx.Response(
            200,
            json={
                "records": [record.model_dump(mode="json") for record in selected],
                "next_page": page + 1 if start + size < len(records) else None,
            },
        )

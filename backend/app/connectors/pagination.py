import asyncio
import base64
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import Field, ValidationError

from app.connectors.http import ConnectorHttp, RunContext
from app.connectors.types import ConnectorError, ErrorCode, ExtractionResult, FrozenModel

MAX_PAGE_NUMBER = 10_000


class Cursor(FrozenModel):
    version: int = 1
    source: str = Field(max_length=160)
    page: int = Field(ge=1, le=MAX_PAGE_NUMBER)
    since: str | None = Field(max_length=40)
    page_size: int = Field(default=50, ge=1, le=100)


def timestamp(value: object) -> datetime:
    if (
        not isinstance(value, str)
        or len(value) > 40
        or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
        )
    ):
        raise ConnectorError(ErrorCode.RECORD)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ConnectorError(ErrorCode.RECORD) from None


def watermark(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ConnectorError(ErrorCode.CONFIGURATION)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def resume(source: str, cursor: str | None, since: datetime | None, page_size: int) -> Cursor:
    selected_since = watermark(since)
    if cursor is None:
        return Cursor(source=source, page=1, since=selected_since, page_size=page_size)
    if len(cursor) > 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
        raise ConnectorError(ErrorCode.CURSOR)
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        decoded = Cursor.model_validate(json.loads(raw))
        if decoded.version != 1 or decoded.source != source or decoded.page_size != page_size:
            raise ValueError
        if decoded.since is not None:
            timestamp(decoded.since)
        if selected_since is not None and selected_since != decoded.since:
            raise ValueError
        return decoded
    except (ValueError, ValidationError, ConnectorError, UnicodeError, RecursionError):
        raise ConnectorError(ErrorCode.CURSOR) from None


def prepare_cursor(
    run: RunContext, source: str, cursor: str | None, since: datetime | None, page_size: int
) -> Cursor:
    run.emit("connector_started")
    try:
        return resume(source, cursor, since, page_size)
    except ConnectorError as error:
        run.emit("connector_failed", code=error.code)
        raise


def encode_cursor(cursor: Cursor, page: int) -> str:
    raw = cursor.model_copy(update={"page": page}).model_dump_json().encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclass(frozen=True)
class Page[T]:
    records: tuple[T, ...]
    consumed: int
    next_page: int | None


async def collect[T](
    http: ConnectorHttp, run: RunContext, cursor: Cursor, fetch: Callable[[int], Awaitable[Page[T]]]
) -> ExtractionResult[T]:
    records: list[T] = []
    consumed = pages = 0
    current = cursor.page
    seen: set[int] = set()
    try:
        async with asyncio.timeout(http.policy.extraction_timeout):
            while True:
                if current in seen or not 1 <= current <= MAX_PAGE_NUMBER:
                    raise ConnectorError(ErrorCode.PAGINATION)
                seen.add(current)
                page = await fetch(current)
                pages += 1
                consumed += page.consumed
                if page.consumed > http.policy.page_size or consumed > http.policy.max_records:
                    raise ConnectorError(ErrorCode.RECORD_LIMIT)
                records.extend(page.records)
                following = page.next_page
                if following is not None and (following <= current or following > MAX_PAGE_NUMBER):
                    raise ConnectorError(ErrorCode.PAGINATION)
                if following is None:
                    continuation = None
                    break
                if (
                    pages >= http.policy.max_pages
                    or consumed + http.policy.page_size > http.policy.max_records
                ):
                    continuation = encode_cursor(cursor, following)
                    break
                current = following
        run.emit("connector_completed", page=current, count=len(records))
        return ExtractionResult(
            records=tuple(records), next_cursor=continuation, pages_fetched=pages
        )
    except TimeoutError:
        run.emit("connector_failed", page=current, count=len(records), code=ErrorCode.DEADLINE)
        raise ConnectorError(ErrorCode.DEADLINE) from None
    except asyncio.CancelledError:
        run.emit("connector_failed", page=current, count=len(records), code=ErrorCode.CANCELLED)
        raise
    except ConnectorError as error:
        run.emit("connector_failed", page=current, count=len(records), code=error.code)
        raise

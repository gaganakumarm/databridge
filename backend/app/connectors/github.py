import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

from pydantic import ValidationError

from app.connectors.http import ConnectorHttp, RunContext
from app.connectors.pagination import Cursor, Page, collect, prepare_cursor, timestamp
from app.connectors.types import (
    ConnectorError,
    ConnectorKind,
    ErrorCode,
    ExtractionResult,
    GitHubConfig,
    Issue,
)


def next_link(header: str | None) -> str | None:
    if header is None:
        return None
    following: str | None = None
    for part in re.split(r",\s*(?=<)", header):
        match = re.fullmatch(r"\s*<([^<>]+)>(.*)", part)
        if match is None:
            raise ConnectorError(ErrorCode.PAGINATION)
        relation = re.search(r';\s*rel\s*=\s*(?:"([^"]*)"|([^;\s]+))', match[2])
        if relation is not None and "next" in (relation[1] or relation[2]).split():
            if following is not None:
                raise ConnectorError(ErrorCode.PAGINATION)
            following = match[1]
    return following


class GitHubIssues:
    def __init__(
        self, config: GitHubConfig, http: ConnectorHttp, *, request_id: UUID | None = None
    ) -> None:
        self.config, self.http, self.request_id = config, http, request_id
        self.path = f"/repos/{config.owner}/{config.repository}/issues"
        self.source = f"github:{config.owner}/{config.repository}"

    def _parameters(self, cursor: Cursor, page: int) -> dict[str, str]:
        params = {
            "state": "all",
            "sort": "updated",
            "direction": "asc",
            "per_page": str(self.http.policy.page_size),
            "page": str(page),
        }
        if cursor.since is not None:
            params["since"] = cursor.since
        return params

    def _next_page(self, link: str | None, cursor: Cursor, current: int) -> int | None:
        following = next_link(link)
        if following is None:
            return None
        try:
            parsed = urlsplit(following)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "api.github.com"
                or parsed.path != self.path
                or parsed.fragment
            ):
                raise ValueError
            pairs = parse_qsl(
                parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8
            )
            params = dict(pairs)
            if len(params) != len(pairs) or not re.fullmatch(
                r"[1-9][0-9]{0,4}", params.get("page", "")
            ):
                raise ValueError
            number = int(params["page"])
            if params != self._parameters(cursor, number) or number != current + 1:
                raise ValueError
            return number
        except (ValueError, UnicodeError):
            raise ConnectorError(ErrorCode.PAGINATION) from None

    def _issue(self, value: object) -> Issue | None:
        if not isinstance(value, dict):
            raise ConnectorError(ErrorCode.RECORD)
        if "pull_request" in value:
            return None
        labels = value.get("labels")
        if not isinstance(labels, list) or len(labels) > 100:
            raise ConnectorError(ErrorCode.RECORD)
        names: list[str] = []
        for label in labels:
            if isinstance(label, str):
                name = label
            elif isinstance(label, dict) and isinstance(label.get("name"), str):
                name = label["name"]
            else:
                raise ConnectorError(ErrorCode.RECORD)
            if len(name) > 100:
                raise ConnectorError(ErrorCode.RECORD)
            names.append(name)
        try:
            issue = Issue(
                id=value["id"],
                number=value["number"],
                title=value["title"],
                state=value["state"],
                created_at=timestamp(value["created_at"]),
                updated_at=timestamp(value["updated_at"]),
                closed_at=timestamp(value["closed_at"])
                if value.get("closed_at") is not None
                else None,
                labels=tuple(names),
                url=value["html_url"],
            )
            expected = f"https://github.com/{self.config.owner}/{self.config.repository}/issues/{issue.number}"
            if issue.url.casefold() != expected.casefold():
                raise ValueError
            return issue
        except (KeyError, ValidationError, ValueError):
            raise ConnectorError(ErrorCode.RECORD) from None

    async def extract(
        self, *, cursor: str | None = None, since: datetime | None = None
    ) -> ExtractionResult[Issue]:
        run = self.http.run(ConnectorKind.GITHUB, self.request_id)
        state = prepare_cursor(run, self.source, cursor, since, self.http.policy.page_size)

        async def fetch(page: int) -> Page[Issue]:
            return await self._fetch(page, state, run)

        return await collect(self.http, run, state, fetch)

    async def _fetch(self, page: int, cursor: Cursor, run: RunContext) -> Page[Issue]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
        if self.config.token is not None:
            headers["Authorization"] = f"Bearer {self.config.token.get_secret_value()}"
        url = "https://api.github.com" + self.path + "?" + urlencode(self._parameters(cursor, page))
        response = await self.http.get_json(url, run, page, headers=headers)
        if not isinstance(response.payload, list):
            raise ConnectorError(ErrorCode.PAYLOAD)
        if len(response.payload) > self.http.policy.page_size:
            raise ConnectorError(ErrorCode.RECORD_LIMIT)
        records = tuple(
            issue for value in response.payload if (issue := self._issue(value)) is not None
        )
        following = self._next_page(response.link, cursor, page)
        run.emit("connector_page_fetched", page=page, count=len(records), category="http_2xx")
        return Page(records, len(response.payload), following)

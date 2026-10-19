from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, field_validator


class ConnectorKind(StrEnum):
    GITHUB = "github_issues"
    MOCK = "mock_saas"


class ErrorCode(StrEnum):
    CONFIGURATION = "invalid_configuration"
    CURSOR = "invalid_cursor"
    AUTHENTICATION = "authentication_failed"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    HTTP = "http_error"
    REDIRECT = "redirect_rejected"
    RETRIES = "retry_exhausted"
    TRANSPORT = "transport_error"
    PAYLOAD = "malformed_payload"
    RECORD = "malformed_record"
    CONTENT_TYPE = "invalid_content_type"
    ENCODING = "unsupported_encoding"
    BYTES = "response_too_large"
    RECORD_LIMIT = "record_limit"
    PAGINATION = "invalid_pagination"
    DEADLINE = "deadline_exceeded"
    CANCELLED = "cancelled"
    LIFECYCLE = "client_not_open"


class ConnectorError(Exception):
    def __init__(self, code: ErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True, strict=True)


class HttpPolicy(FrozenModel):
    connect_timeout: float = Field(default=5.0, gt=0, le=30)
    read_timeout: float = Field(default=10.0, gt=0, le=30)
    write_timeout: float = Field(default=5.0, gt=0, le=30)
    pool_timeout: float = Field(default=5.0, gt=0, le=30)
    extraction_timeout: float = Field(default=60.0, gt=0, le=300)
    max_connections: int = Field(default=5, ge=1, le=10)
    max_response_bytes: int = Field(default=1_048_576, ge=1, le=8_388_608)
    page_size: int = Field(default=50, ge=1, le=100)
    max_pages: int = Field(default=10, ge=1, le=100)
    max_records: int = Field(default=1000, ge=1, le=10_000)
    max_attempts: int = Field(default=4, ge=1, le=8)
    base_delay: float = Field(default=0.5, ge=0, le=10)
    max_backoff: float = Field(default=10.0, ge=0, le=30)
    max_total_delay: float = Field(default=30.0, ge=0, le=120)
    jitter: float = Field(default=0.25, ge=0, le=1)


class GitHubConfig(FrozenModel):
    owner: str = Field(min_length=1, max_length=39, pattern=r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
    repository: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    token: SecretStr | None = None

    @field_validator("repository")
    @classmethod
    def repository_identifier(cls, value: str) -> str:
        if value in {".", ".."}:
            raise ValueError("Invalid repository identifier")
        return value

    @field_validator("token")
    @classmethod
    def token_header(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            raw = value.get_secret_value()
            if not raw or len(raw) > 512 or any(not 33 <= ord(char) <= 126 for char in raw):
                raise ValueError("Invalid token")
        return value


class MockConfig(FrozenModel):
    endpoint: Literal["https://mock.databridge.test/v1/records"] = (
        "https://mock.databridge.test/v1/records"
    )
    username: SecretStr
    password: SecretStr

    @field_validator("username", "password")
    @classmethod
    def credentials(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw or len(raw) > 256 or any(ord(char) < 32 for char in raw):
            raise ValueError("Invalid test credentials")
        return value

    @field_validator("username")
    @classmethod
    def basic_username(cls, value: SecretStr) -> SecretStr:
        if ":" in value.get_secret_value():
            raise ValueError("Invalid Basic Auth username")
        return value


class Issue(FrozenModel):
    id: int = Field(gt=0, le=9_223_372_036_854_775_807)
    number: int = Field(gt=0, le=2_147_483_647)
    title: str = Field(max_length=1024)
    state: Literal["open", "closed"]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    closed_at: AwareDatetime | None
    labels: tuple[str, ...]
    url: str


class MockRecord(FrozenModel):
    external_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    updated_at: AwareDatetime
    name: str = Field(max_length=256)


class ExtractionResult[T](FrozenModel):
    records: tuple[T, ...]
    next_cursor: str | None
    pages_fetched: int


class SourceConnector[T](Protocol):
    async def extract(
        self, *, cursor: str | None = None, since: datetime | None = None
    ) -> ExtractionResult[T]: ...

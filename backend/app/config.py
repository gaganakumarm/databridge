from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATABRIDGE_", env_file=".env", extra="forbid", hide_input_in_errors=True
    )

    database_url: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            try:
                parsed = make_url(value.get_secret_value())
                if (
                    parsed.drivername != "postgresql+psycopg"
                    or not parsed.host
                    or not parsed.database
                ):
                    raise ValueError
            except Exception:
                raise ValueError("Use a postgresql+psycopg URL with host and database") from None
        return value

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_directory: str = Field(default="logs", min_length=1)
    log_max_bytes: int = Field(default=1_048_576, ge=1024, le=104_857_600)
    log_backup_count: int = Field(default=3, ge=1, le=10)
    log_to_file: bool = True

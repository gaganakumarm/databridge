from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATABRIDGE_", env_file=".env", extra="forbid", hide_input_in_errors=True
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_directory: str = Field(default="logs", min_length=1)
    log_max_bytes: int = Field(default=1_048_576, ge=1024, le=104_857_600)
    log_backup_count: int = Field(default=3, ge=1, le=10)
    log_to_file: bool = True

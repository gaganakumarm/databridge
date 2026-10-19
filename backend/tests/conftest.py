"""Integration isolation: one generated schema per test in a dedicated *_test database."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.persistence.database import Database

BACKEND = Path(__file__).resolve().parents[1]


def migration_config() -> Config:
    return Config(str(BACKEND / "alembic.ini"))


@pytest.fixture
def database() -> Iterator[Database]:
    url = os.environ.get("DATABRIDGE_TEST_DATABASE_URL")
    if not url or not (make_url(url).database or "").endswith("_test"):
        pytest.fail("Set DATABRIDGE_TEST_DATABASE_URL to a dedicated PostgreSQL *_test database")
    settings = Settings(database_url=SecretStr(url), log_to_file=False)
    administrator = Database(settings)
    schema = "test_" + uuid4().hex
    database = Database(settings)
    with administrator.engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    @event.listens_for(database.engine, "connect")
    def search_path(dbapi_connection: Any, _record: Any) -> None:
        # Identifier is generated here, never supplied by a caller.
        dbapi_connection.autocommit = True
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        dbapi_connection.autocommit = False

    try:
        with database.engine.begin() as connection:
            config = migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield database
    finally:
        database.dispose()
        with administrator.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        administrator.dispose()

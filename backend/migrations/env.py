"""Database URL comes from validated settings, never Alembic's log configuration."""

from alembic import context

from app.config import Settings
from app.persistence.database import Database
from app.persistence.models import Base


def run_migrations() -> None:
    supplied_connection = context.config.attributes.get("connection")
    if supplied_connection is not None:
        context.configure(
            connection=supplied_connection, target_metadata=Base.metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    settings = Settings()
    if context.is_offline_mode():
        if settings.database_url is None:
            raise ValueError("Database URL is required for migrations")
        context.configure(
            url=settings.database_url.get_secret_value(),
            target_metadata=Base.metadata,
            literal_binds=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    else:
        database = Database(settings)
        try:
            # Migrations use an isolated process and do not share application sessions.
            with database.engine.connect() as connection:
                context.configure(
                    connection=connection, target_metadata=Base.metadata, compare_type=True
                )
                with context.begin_transaction():
                    context.run_migrations()
        finally:
            database.dispose()


run_migrations()

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.logging import configure_logging
from app.persistence.database import Database


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["databridge"] = "databridge"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, logger: logging.Logger) -> None:
        self.app = app
        self.logger = logger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Generate server-owned IDs; never trust or log caller-provided identifiers.
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = perf_counter()
        status = 500
        response_started = False

        async def correlated_send(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        failed = False
        try:
            await self.app(scope, receive, correlated_send)
        except Exception:
            failed = True
            if not response_started:
                await correlated_send(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {"type": "http.response.body", "body": b'{"detail":"Internal server error"}'}
                )
        finally:
            self.logger.log(
                logging.ERROR if failed else logging.INFO,
                "request_failed" if failed else "request_completed",
                extra={
                    "request_id": request_id,
                    "status_code": status,
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                },
            )


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()
    logger = configure_logging(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(config) if config.database_url is not None else None
        app.state.database = database
        try:
            yield
        finally:
            if database is not None:
                database.dispose()

    application = FastAPI(title="DataBridge", debug=False, lifespan=lifespan)
    application.add_middleware(RequestContextMiddleware, logger=logger)

    @application.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse()

    return application

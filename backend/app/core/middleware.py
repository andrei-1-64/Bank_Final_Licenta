"""CORS, request-id, error mapping, and a simple in-process rate limiter.

Wired onto the app in main.py::create_app.
"""

import logging
import time
import uuid
from collections import defaultdict, deque

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.core.exceptions import AppError, RateLimitExceededError

logger = logging.getLogger("app")

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        response: Response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window-ish limiter keyed by client IP, kept entirely in
    process memory. Fine for a single-instance demo app; a real deployment
    with multiple backend processes would need a shared store (e.g. Redis)
    since this state does not survive a restart or scale-out."""

    def __init__(self, app, requests_per_minute: int):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window_start = now - 60.0

        hits = self._hits[client_ip]
        while hits and hits[0] < window_start:
            hits.popleft()

        if len(hits) >= self.requests_per_minute:
            error = RateLimitExceededError()
            return JSONResponse(
                status_code=error.status_code,
                content={"error": {"code": error.error_code, "message": error.message}},
            )

        hits.append(now)
        return await call_next(request)


def configure_middleware(app: FastAPI) -> None:
    # Starlette's add_middleware() makes each new call the OUTERMOST layer
    # (it prepends to the stack), so registration order here is innermost
    # to outermost. CORSMiddleware must be added LAST: RateLimitMiddleware
    # short-circuits with its own JSONResponse before calling `call_next`,
    # and a response built INSIDE CORSMiddleware never passes back through
    # it to pick up CORS headers - a rate-limited browser request used to
    # come back as an unreadable "blocked by CORS policy" instead of the
    # actual 429 body.
    app.add_middleware(RateLimitMiddleware, requests_per_minute=settings.RATE_LIMIT_PER_MINUTE)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        error_body: dict = {"code": exc.error_code, "message": exc.message}
        if exc.details is not None:
            error_body["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content={"error": error_body})

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # jsonable_encoder, not exc.errors() directly. Pydantic v2 can put
        # non-serialisable objects in an error entry, and plain JSONResponse
        # then blows up *inside this handler*, turning a client's 422 into a
        # 500. Two ways in, both real and both fixed by the same call:
        #   * a malformed body (e.g. missing/wrong Content-Type) puts the raw
        #     request bytes in the error's "input" field - this is what
        #     produced the browser's "Failed to fetch";
        #   * a @field_validator that raises ValueError puts the exception
        #     object itself in the error's "ctx".
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Invalid request.",
                    "details": jsonable_encoder(exc.errors()),
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.exception("Unhandled error (request_id=%s)", request_id)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "Something went wrong."}},
        )

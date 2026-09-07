import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import api_router
from app.core.middleware import configure_middleware, register_exception_handlers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield


def create_app() -> FastAPI:
    # uvicorn's --log-level only configures its own loggers. Everything the app
    # logs via logging.getLogger(__name__) propagates to the root logger, which
    # otherwise has no handler and sits at WARNING - silently dropping the
    # agent's INFO tool-loop trace. This is the one place that fixes that.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = FastAPI(title="BanK API", lifespan=lifespan)

    configure_middleware(app)
    register_exception_handlers(app)

    app.include_router(api_router, prefix="/api/v1")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()

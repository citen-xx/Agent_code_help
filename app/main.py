"""FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.agent import router as agent_router
from app.api.health import router as health_router
from app.api.workspaces import router as workspaces_router
from app.core.config import settings
from app.core.logging import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting %s (env=%s)", settings.APP_NAME, settings.APP_ENV)
    yield
    logger.info("Shutting down %s", settings.APP_NAME)


def create_app() -> FastAPI:
    """Application factory (also used by the test suite)."""
    setup_logging()
    app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(agent_router)
    app.include_router(workspaces_router)
    return app


app = create_app()

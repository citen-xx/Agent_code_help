"""Shared Pydantic schemas for the HTTP API."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response body of ``GET /health``."""

    status: str
    service: str

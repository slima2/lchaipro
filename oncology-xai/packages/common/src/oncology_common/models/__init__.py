"""Pydantic domain models."""

from oncology_common.models.base import (
    BaseModel,
    BaseResponse,
    ErrorResponse,
    HealthResponse,
    PaginatedResponse,
)
from oncology_common.models.entities import *  # noqa: F403  -- reexporta las ~35 entidades del dominio

__all__ = [
    "BaseModel",
    "BaseResponse",
    "ErrorResponse",
    "HealthResponse",
    "PaginatedResponse",
]

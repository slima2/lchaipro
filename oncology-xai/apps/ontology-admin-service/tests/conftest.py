"""Pytest fixtures para ontology-admin-service (F6.6)."""

from __future__ import annotations

import os
import sys
import types
from typing import AsyncIterator

os.environ.setdefault("POSTGRES_DSN", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("POSTGRES_DSN_SYNC", "sqlite:///:memory:")
os.environ.setdefault("RABBITMQ_URL", "amqp://user:pass@localhost:5672/")

# ``app.literature_search`` llama a httpx hacia PubMed / arXiv al
# importarse si no stubeamos. No lo necesitamos para los tests de
# autorización / validación.
_fake_lit = types.ModuleType("app.literature_search")
_fake_lit.DEFAULT_QUERIES = []  # type: ignore[attr-defined]


async def _fake_batch(*_args, **_kwargs):
    return []


_fake_lit.batch_literature_search = _fake_batch  # type: ignore[attr-defined]
sys.modules["app.literature_search"] = _fake_lit


import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from oncology_common.database import Base  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: E402,F401  (registra las tablas)
from app.database import get_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402


@pytest.fixture
async def async_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def client(async_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield async_session

    fastapi_app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    fastapi_app.dependency_overrides.clear()

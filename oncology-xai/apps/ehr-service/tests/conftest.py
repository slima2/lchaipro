"""Pytest fixtures para ehr-service (F6.3)."""

from __future__ import annotations

import os
from typing import AsyncIterator

os.environ.setdefault("POSTGRES_DSN", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("POSTGRES_DSN_SYNC", "sqlite:///:memory:")
os.environ.setdefault("RABBITMQ_URL", "amqp://user:pass@localhost:5672/")

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from oncology_common.database import Base  # noqa: E402
from sqlalchemy import event  # noqa: E402
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

    # SQLite no respeta ``ON DELETE CASCADE`` por defecto — hay que
    # pedir foreign_keys=ON por conexión. Esto nos permite probar la
    # cascada en tests sin un Postgres real.
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_conn, _):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

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

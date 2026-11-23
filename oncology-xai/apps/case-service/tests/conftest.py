"""Pytest fixtures for case-service tests.

Levanta la app FastAPI contra SQLite in-memory, mockeando RabbitMQ (no se
necesita bus real para los tests de autorización). Los settings se parchean
antes de importar la app para que ``Settings`` no exija ``postgres_dsn`` /
``rabbitmq_url`` reales.
"""

from __future__ import annotations

import os
from typing import AsyncIterator

# ── Config env ANTES de importar la app ────────────────────────────────
os.environ.setdefault("POSTGRES_DSN", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("POSTGRES_DSN_SYNC", "sqlite:///:memory:")
os.environ.setdefault("RABBITMQ_URL", "amqp://user:pass@localhost:5672/")

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from oncology_common.database import Base  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: E402,F401  (registra las tablas en el metadata)
from app import routes  # noqa: E402
from app.database import get_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402


@pytest.fixture
async def async_session() -> AsyncIterator[AsyncSession]:
    # ``:memory:`` + ``StaticPool`` mantiene UNA sola conexión viva durante el
    # test, requisito para que app y fixture compartan la DB.
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
async def client(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    # Mock del publisher: no queremos RabbitMQ real en tests
    monkeypatch.setattr(routes, "_publish_audit", lambda *a, **k: None)
    # F6.5 — mock del storage cleanup: tests no tienen S3 real.
    # Devuelve la misma shape que la función real (ok + 0 objetos).
    monkeypatch.setattr(
        routes,
        "_cleanup_case_storage",
        lambda case_id: {"storage_deleted": 0, "storage_cleanup_ok": True},
    )

    async def _override() -> AsyncIterator[AsyncSession]:
        yield async_session

    fastapi_app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    fastapi_app.dependency_overrides.clear()

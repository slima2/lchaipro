"""Pytest fixtures para inference-service.

Levanta la app FastAPI contra SQLite in-memory + tabla ``images`` mínima.
Los tests mockean ``process_image_task`` (Celery) y ``celery_app.celery``
(usado por ``cancel_job``) para no necesitar Redis/broker real.
"""

from __future__ import annotations

import os
import sys
import types
from typing import AsyncIterator

# ── Config env ANTES de importar la app ────────────────────────────────
os.environ.setdefault("POSTGRES_DSN", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("POSTGRES_DSN_SYNC", "sqlite:///:memory:")
os.environ.setdefault("RABBITMQ_URL", "amqp://user:pass@localhost:5672/")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")
os.environ.setdefault("S3_ENDPOINT", "http://localhost:9000")
os.environ.setdefault("S3_ACCESS_KEY", "test")
os.environ.setdefault("S3_SECRET_KEY", "test")
os.environ.setdefault("S3_BUCKET", "test")

# ── Stubs para evitar imports pesados (torch / openslide / celery real) ──
#
# ``app.tasks`` importa el pipeline de inferencia (torch, openslide, etc.).
# Para los tests de autorización stubamos todo el módulo con un dispatcher
# en memoria que solo guarda el call y retorna un fake AsyncResult.

_dispatched_calls: list[tuple] = []


class _FakeAsync:
    id = "fake-task-id"


class _FakeTask:
    def delay(self, *args, **kwargs):  # noqa: D401
        _dispatched_calls.append(("delay", args, kwargs))
        return _FakeAsync()


_fake_tasks_module = types.ModuleType("app.tasks")
_fake_tasks_module.process_image_task = _FakeTask()  # type: ignore[attr-defined]
sys.modules["app.tasks"] = _fake_tasks_module


class _FakeControl:
    def revoke(self, *_args, **_kwargs):
        return None


class _FakeCelery:
    control = _FakeControl()


_fake_celery_module = types.ModuleType("app.celery_app")
_fake_celery_module.celery = _FakeCelery()  # type: ignore[attr-defined]
sys.modules["app.celery_app"] = _fake_celery_module


import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from oncology_common.database import Base  # noqa: E402
from sqlalchemy import Column, DateTime, Integer, String, text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: E402,F401
from app.database import get_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402


# El inference-service no define ImageDB; solo lo consulta vía SQL.
# Para los tests declaramos la tabla "images" en el mismo metadata para
# que SQLAlchemy la cree junto con el resto.
class _ImageStub(Base):
    __tablename__ = "images"
    id = Column(String(36), primary_key=True)
    case_id = Column(String(36), nullable=False, index=True)
    format = Column(String(10), nullable=False, default="png")
    storage_uri = Column(String(1024), nullable=False)
    checksum = Column(String(64), nullable=False)
    size_bytes = Column(Integer, nullable=False, default=0)
    uploaded_by = Column(String(255), nullable=True)
    uploaded_at = Column(DateTime(timezone=True), nullable=True)


@pytest.fixture
def dispatched_calls() -> list[tuple]:
    _dispatched_calls.clear()
    return _dispatched_calls


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
async def client(
    async_session: AsyncSession,
) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield async_session

    fastapi_app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    fastapi_app.dependency_overrides.clear()


async def seed_image(
    session: AsyncSession,
    *,
    image_id: str,
    case_id: str,
    confirmed: bool = True,
    uploaded_by: str = "clinician1",
) -> None:
    checksum = "deadbeef" if confirmed else "pending-upload"
    await session.execute(
        text(
            "INSERT INTO images (id, case_id, format, storage_uri, checksum, "
            "size_bytes, uploaded_by) VALUES "
            "(:id, :cid, 'png', :uri, :chk, 100, :by)"
        ),
        {
            "id": image_id,
            "cid": case_id,
            "uri": f"s3://bucket/images/{case_id}/{image_id}.png",
            "chk": checksum,
            "by": uploaded_by,
        },
    )
    await session.commit()

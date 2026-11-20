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


# ``app.ml.checkpoints.loader`` importa torch en el top-level. Lo
# stubeamos para que los tests de ``PUT /parameters`` (F6.4) no
# requieran torch. Sus constantes son las que el endpoint muta.
_fake_loader = types.ModuleType("app.ml.checkpoints.loader")
_fake_loader.PROPOSED_AUROC = {"EGFR": 0.82, "KRAS": 0.80, "TP53": 0.78}  # type: ignore[attr-defined]
_fake_loader.AUROC_THRESHOLD = 0.70  # type: ignore[attr-defined]
_fake_loader.BEST_METHOD = {"EGFR": "abmil", "KRAS": "choquet", "TP53": "xgboost"}  # type: ignore[attr-defined]
_fake_loader.BEST_FOLD = {"EGFR": 0}  # type: ignore[attr-defined]
_fake_loader.GENE_MUTATION_THRESHOLD = {"EGFR": 0.6, "KRAS": 0.6, "TP53": 0.6}  # type: ignore[attr-defined]


class _FakeCheckpointLoader:
    def __init__(self, *a, **k):
        pass

    def get_checkpoint_status(self):
        return {"status": "stub"}


_fake_loader.CheckpointLoader = _FakeCheckpointLoader  # type: ignore[attr-defined]
sys.modules["app.ml.checkpoints"] = types.ModuleType("app.ml.checkpoints")
sys.modules["app.ml"] = types.ModuleType("app.ml")
sys.modules["app.ml.checkpoints.loader"] = _fake_loader


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


# El inference-service no define ImageDB ni CaseDB; solo las consulta via
# SQL. Para los tests las declaramos en el mismo metadata para que
# SQLAlchemy las cree junto con el resto.
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


class _CaseStub(Base):
    """Stub mínimo de ``cases`` para que ``require_case_access`` tenga
    sobre qué consultar en los tests de ownership (F6.5)."""

    __tablename__ = "cases"
    id = Column(String(36), primary_key=True)
    patient_id = Column(String(36), nullable=True)
    created_by = Column(String(255), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


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


async def seed_case(
    session: AsyncSession,
    *,
    case_id: str,
    created_by: str = "clinicianA",
    deleted: bool = False,
) -> None:
    """Crea una row en ``cases`` para los ownership checks de F6.5."""
    await session.execute(
        text(
            "INSERT INTO cases (id, patient_id, created_by, deleted_at) "
            "VALUES (:id, 'p1', :by, :del)"
        ),
        {
            "id": case_id,
            "by": created_by,
            "del": "2026-11-19 00:00:00" if deleted else None,
        },
    )
    await session.commit()


async def seed_image(
    session: AsyncSession,
    *,
    image_id: str,
    case_id: str,
    confirmed: bool = True,
    uploaded_by: str = "clinicianA",
    with_case: bool = True,
    case_created_by: str = "clinicianA",
) -> None:
    """Inserta una imagen; por defecto también inserta su caso para que
    ``require_case_access`` tenga una row sobre la cual decidir."""
    if with_case:
        # idempotente: SQLite no soporta ``ON CONFLICT`` sin schema,
        # chequeamos a mano.
        exists = (
            await session.execute(
                text("SELECT 1 FROM cases WHERE id = :c"), {"c": case_id}
            )
        ).fetchone()
        if not exists:
            await seed_case(
                session, case_id=case_id, created_by=case_created_by
            )

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

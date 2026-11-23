"""Pytest fixtures para image-service (F6.7)."""

from __future__ import annotations

import os
from typing import AsyncIterator

os.environ.setdefault("POSTGRES_DSN", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("POSTGRES_DSN_SYNC", "sqlite:///:memory:")
os.environ.setdefault("RABBITMQ_URL", "amqp://user:pass@localhost:5672/")

# ``app.storage`` toca boto3 en import tiempo; stubeamos lo necesario.
import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from oncology_common.database import Base  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: E402,F401
from app.database import get_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402


class _FakeStorage:
    """Backend de objetos stub para tests F6.7.

    Registra las llamadas a ``create_multipart_upload`` y
    ``abort_multipart_upload`` para que los tests verifiquen que:
     * no se crea un multipart si la petición es rechazada por tope;
     * un abort explícito llega al backend.
    """

    def __init__(self) -> None:
        self.created: list[str] = []
        self.aborted: list[tuple[str, str]] = []
        self.put_urls_generated = 0
        self.part_urls_generated = 0

    def create_multipart_upload(self, key: str, content_type: str = "") -> str:
        upload_id = f"mpu-{len(self.created) + 1}"
        self.created.append(upload_id)
        return upload_id

    def presigned_upload_part(
        self, key: str, upload_id: str, part_number: int, expires_in: int = 7200
    ) -> str:
        self.part_urls_generated += 1
        return f"http://fake/{key}?partNumber={part_number}"

    def presigned_put_url(
        self, key: str, content_type: str = "", expires_in: int = 7200
    ) -> str:
        self.put_urls_generated += 1
        return f"http://fake/{key}"

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        self.aborted.append((key, upload_id))

    def head_object(self, key: str) -> dict:
        return {"ContentLength": 123, "ETag": '"xyz"'}


@pytest.fixture
def fake_storage(monkeypatch) -> _FakeStorage:
    fs = _FakeStorage()
    import app.routes as routes

    monkeypatch.setattr(routes, "_storage", lambda: fs)
    # Reset del rate-limit entre tests
    routes._upload_rate_hits.clear()
    return fs


@pytest.fixture(autouse=True)
def _silence_audit(monkeypatch):
    """No publicar a RabbitMQ en tests."""
    import app.routes as routes

    monkeypatch.setattr(routes, "_publish_audit", lambda *a, **kw: None)


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
        # ``cases`` vive en case-service pero ``require_case_access``
        # lee ``created_by`` + ``deleted_at`` vía SQL puro; creamos una
        # tabla mínima para que el test pueda sembrar un caso dueño.
        await conn.execute(
            text(
                "CREATE TABLE cases ("
                " id TEXT PRIMARY KEY,"
                " created_by TEXT,"
                " deleted_at TIMESTAMP"
                ")"
            )
        )
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


async def seed_case(session: AsyncSession, case_id: str, owner: str) -> None:
    await session.execute(
        text("INSERT INTO cases (id, created_by, deleted_at) VALUES (:i, :o, NULL)"),
        {"i": case_id, "o": owner},
    )
    await session.commit()

"""Database setup for inference-service."""

from oncology_common.database import create_db_engine, create_session_factory
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings

engine = create_db_engine(settings.postgres_dsn)
async_session = create_session_factory(engine)

# Sync engine for Celery workers
sync_engine = create_engine(settings.postgres_dsn_sync)


async def get_db():
    async with async_session() as session:
        yield session


def get_sync_db() -> Session:
    return Session(sync_engine)

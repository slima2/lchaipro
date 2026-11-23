"""SQLAlchemy models for images."""

import uuid
from datetime import datetime

from oncology_common.database import Base
from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column


class ImageDB(Base):
    __tablename__ = "images"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    case_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    format: Mapped[str] = mapped_column(String(10), nullable=False, default="png")
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stain: Mapped[str | None] = mapped_column(String(100))
    magnification: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(String(2000))
    uploaded_by: Mapped[str | None] = mapped_column(String(255))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    # F6.5 — soft-delete propagado desde la saga de case.deleted. Permite
    # ocultar la imagen de lecturas sin perder la trazabilidad (hash,
    # tamaño, uploader) necesaria para auditoría.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delete_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

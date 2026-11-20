"""SQLAlchemy models for EHR documents, entities, mappings."""

import uuid
from datetime import datetime

from oncology_common.database import Base
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column


class EHRDocumentDB(Base):
    __tablename__ = "ehr_documents"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    case_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(50), default="paste")
    content_text: Mapped[str | None] = mapped_column(Text)
    content_uri: Mapped[str | None] = mapped_column(String(1024))
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )


class EHREntityDB(Base):
    __tablename__ = "ehr_entities"
    __table_args__ = (
        # F6.3 — dedupe por tramo+tipo. Impide que ``:extract-and-map``
        # inserte el mismo match dos veces si se llama varias veces sobre
        # el mismo documento.
        UniqueConstraint(
            "ehr_id", "start", "end", "entity_type", name="uq_ehr_entity_span"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # F6.3 — ON DELETE CASCADE: al borrar el documento desaparece todo
    # su tree (entities + mappings) sin orphans que luego rompan los
    # JOINs del dashboard.
    ehr_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("ehr_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    text: Mapped[str] = mapped_column(String(500), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    start: Mapped[int] = mapped_column(Integer, nullable=False)
    end: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    section: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )


class EHRMappingDB(Base):
    __tablename__ = "ehr_mappings"
    __table_args__ = (
        # F6.3 — una entidad no debería mapearse al mismo IRI dos veces
        # (keyword_lookup determinístico). Impide que re-ejecuciones
        # multipliquen filas.
        UniqueConstraint("entity_id", "ontology", "iri", name="uq_ehr_mapping_iri"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    entity_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("ehr_entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ontology: Mapped[str] = mapped_column(String(20), nullable=False)
    iri: Mapped[str] = mapped_column(String(500), nullable=False)
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    mapping_method: Mapped[str] = mapped_column(String(100), default="keyword_lookup")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

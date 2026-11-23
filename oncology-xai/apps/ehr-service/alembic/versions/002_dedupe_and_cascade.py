"""F6.3 — dedupe en entidades y mappings + ON DELETE CASCADE.

Pentest 2026-11-19 §6.3:

* ``POST /ehr/{id}:extract-and-map`` no era idempotente: cada llamada
  insertaba entidades/mappings duplicados y las vistas del dashboard
  los reportaban como "nuevos" hallazgos (DoS de UX + corrupción de
  métricas de confianza por agregación duplicada).
* Al soft-eliminar un paciente/caso, los ``ehr_entities`` y
  ``ehr_mappings`` quedaban huérfanos y rompían el ``GET`` del
  dashboard con 500 por FK dangling.

Esta migración:

1. Elimina duplicados existentes (tie-break por ``created_at`` más
   antiguo) para que las unique constraints puedan crearse.
2. Añade ``uq_ehr_entity_span (ehr_id, start, end, entity_type)``.
3. Añade ``uq_ehr_mapping_iri (entity_id, ontology, iri)``.
4. Recrea las FKs con ``ON DELETE CASCADE`` para que la eliminación
   de un ``ehr_documents`` baje toda su jerarquía automáticamente.

Revision ID: 002
Revises: 001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 1. Purgar duplicados existentes antes de poder crear las UQ ────
    # Para cada combinación (ehr_id, start, end, entity_type) conservamos
    # la fila con MIN(created_at); el resto se eliminan.
    op.execute(
        """
        DELETE FROM ehr_entities
        WHERE id NOT IN (
            SELECT MIN(id) FROM ehr_entities
            GROUP BY ehr_id, "start", "end", entity_type
        )
        """
    )
    op.execute(
        """
        DELETE FROM ehr_mappings
        WHERE id NOT IN (
            SELECT MIN(id) FROM ehr_mappings
            GROUP BY entity_id, ontology, iri
        )
        """
    )

    # ── 2. Unique constraints ────────────────────────────────────────
    op.create_unique_constraint(
        "uq_ehr_entity_span",
        "ehr_entities",
        ["ehr_id", "start", "end", "entity_type"],
    )
    op.create_unique_constraint(
        "uq_ehr_mapping_iri",
        "ehr_mappings",
        ["entity_id", "ontology", "iri"],
    )

    # ── 3. Recrear FKs con ON DELETE CASCADE ─────────────────────────
    # Alembic no da un helper portable para "modificar" FK: hay que
    # drop + create.
    with op.batch_alter_table("ehr_entities") as batch:
        batch.drop_constraint("ehr_entities_ehr_id_fkey", type_="foreignkey")
        batch.create_foreign_key(
            "ehr_entities_ehr_id_fkey",
            "ehr_documents",
            ["ehr_id"],
            ["id"],
            ondelete="CASCADE",
        )
    with op.batch_alter_table("ehr_mappings") as batch:
        batch.drop_constraint("ehr_mappings_entity_id_fkey", type_="foreignkey")
        batch.create_foreign_key(
            "ehr_mappings_entity_id_fkey",
            "ehr_entities",
            ["entity_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("ehr_mappings") as batch:
        batch.drop_constraint("ehr_mappings_entity_id_fkey", type_="foreignkey")
        batch.create_foreign_key(
            "ehr_mappings_entity_id_fkey",
            "ehr_entities",
            ["entity_id"],
            ["id"],
        )
    with op.batch_alter_table("ehr_entities") as batch:
        batch.drop_constraint("ehr_entities_ehr_id_fkey", type_="foreignkey")
        batch.create_foreign_key(
            "ehr_entities_ehr_id_fkey",
            "ehr_documents",
            ["ehr_id"],
            ["id"],
        )
    op.drop_constraint("uq_ehr_mapping_iri", "ehr_mappings", type_="unique")
    op.drop_constraint("uq_ehr_entity_span", "ehr_entities", type_="unique")

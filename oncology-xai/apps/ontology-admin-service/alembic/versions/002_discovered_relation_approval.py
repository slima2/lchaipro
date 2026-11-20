"""F6.6 — human approval + rollback real en discovered_relations.

Pentest 2026-11-19 §6.6:

1. Las relaciones descubiertas por el pipeline DeepSearch se publicaban
   directamente con ``is_active = True`` y pasaban a estar visibles en
   el KG y en los prompts del LLM del graph-service sin que un humano
   validara que la relación tenía sentido o que el ``evidence_quote``
   realmente la respaldaba.
2. El endpoint ``:rollback`` sólo cambiaba el status de la proposal a
   "ROLLED_BACK" pero las relaciones activadas seguían vivas en la DB.

Esta migración:

* Vuelve inactivas todas las relaciones existentes (``is_active = False``)
  — las reaprobará un admin cuando sea seguro.
* Añade ``approved_by`` / ``approved_at`` / ``rolled_back_by`` /
  ``rolled_back_at`` / ``rollback_reason`` a ``discovered_relations``.
* Crea un índice sobre ``is_active`` (lo pedía el query de graph-service).

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
    op.add_column(
        "discovered_relations",
        sa.Column("approved_by", sa.String(255), nullable=True),
    )
    op.add_column(
        "discovered_relations",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "discovered_relations",
        sa.Column("rolled_back_by", sa.String(255), nullable=True),
    )
    op.add_column(
        "discovered_relations",
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "discovered_relations",
        sa.Column("rollback_reason", sa.String(500), nullable=True),
    )

    # Volver inactivas las relaciones existentes: el fix exige que un
    # humano apruebe cada una.
    op.execute(
        "UPDATE discovered_relations SET is_active = false WHERE is_active = true"
    )

    op.create_index(
        "idx_discovered_relations_active",
        "discovered_relations",
        ["is_active"],
    )


def downgrade() -> None:
    op.drop_index("idx_discovered_relations_active", table_name="discovered_relations")
    op.drop_column("discovered_relations", "rollback_reason")
    op.drop_column("discovered_relations", "rolled_back_at")
    op.drop_column("discovered_relations", "rolled_back_by")
    op.drop_column("discovered_relations", "approved_at")
    op.drop_column("discovered_relations", "approved_by")

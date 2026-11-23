"""F6.2 — Soft-delete para patients y cases.

El pentest de pre-prod (2026-11-19, hallazgo 6.2) confirma que un clinician
puede destruir pacientes/casos completos sin rol admin, sin confirmación y
sin evento de auditoría. La respuesta es:

* restringir el DELETE a rol ``admin`` (api-gateway),
* exigir un motivo (``delete_reason``) y persistir el actor (``deleted_by``),
* reemplazar el hard-delete por soft-delete (``deleted_at IS NOT NULL``)
  excluido en los listados y GET.

Esta migración añade las tres columnas en ``patients`` y ``cases`` + un índice
parcial por ``deleted_at`` para que las consultas "no borrados" sigan siendo
rápidas sobre tablas grandes.

Revision ID: 002
Revises: 001
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("patients", "cases"):
        op.add_column(
            table,
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(
            table, sa.Column("deleted_by", sa.String(255), nullable=True)
        )
        op.add_column(
            table, sa.Column("delete_reason", sa.String(1024), nullable=True)
        )
        op.create_index(
            f"idx_{table}_deleted_at", table, ["deleted_at"]
        )


def downgrade() -> None:
    for table in ("patients", "cases"):
        op.drop_index(f"idx_{table}_deleted_at", table_name=table)
        op.drop_column(table, "delete_reason")
        op.drop_column(table, "deleted_by")
        op.drop_column(table, "deleted_at")

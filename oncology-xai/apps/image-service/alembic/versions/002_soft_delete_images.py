"""F6.5 — soft-delete en images para la saga de case.deleted.

Al soft-eliminar un caso (F6.2), también se oculta su imagen y se borran
los objetos S3 asociados (prefijos ``images/{cid}/`` y ``results/{cid}/``).
Esta columna permite ocultar la imagen en lecturas (``GET /images/...``,
``GET /cases/{cid}/images``) sin perder los metadatos para auditoría.

Revision ID: 002
Revises: 001
"""

import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "images",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("images", sa.Column("deleted_by", sa.String(255), nullable=True))
    op.add_column("images", sa.Column("delete_reason", sa.String(500), nullable=True))
    # Índice parcial para scans "activos" eficientes
    op.create_index(
        "idx_images_case_active",
        "images",
        ["case_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_images_case_active", table_name="images")
    op.drop_column("images", "delete_reason")
    op.drop_column("images", "deleted_by")
    op.drop_column("images", "deleted_at")

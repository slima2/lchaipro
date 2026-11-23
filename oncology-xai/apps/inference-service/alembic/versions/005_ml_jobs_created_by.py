"""F6.1 — ml_jobs.created_by para ownership de cancel.

Pentest 2026-11-19 §6.1: un clinician podía cancelar jobs ajenos y
disparar una cascada destructiva sobre el caso de otro. Con esta
columna el servicio puede validar que ``cancel`` lo llama el creador
del job o un admin.

Revision ID: 005
Revises: 004
Create Date: 2026-11-19
"""

import sqlalchemy as sa
from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ml_jobs",
        sa.Column("created_by", sa.String(255), nullable=True),
    )
    op.create_index("idx_ml_jobs_created_by", "ml_jobs", ["created_by"])


def downgrade() -> None:
    op.drop_index("idx_ml_jobs_created_by", table_name="ml_jobs")
    op.drop_column("ml_jobs", "created_by")

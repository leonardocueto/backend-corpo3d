"""reactivation tokens

Revision ID: 0017_reactivation_tokens
Revises: 0016_user_deactivated_at
Create Date: 2026-10-05

Tabla del token que revive una cuenta dada de baja por su dueno. Es una tabla
APARTE de `password_reset_tokens` y no una columna `purpose` en esa: asi el tipo de
la tabla es el discriminador y un token de reset no puede revivir una cuenta, sin
depender de que ningun `select` futuro se acuerde de filtrar.

Solo AGREGA una tabla: un rollback a una imagen anterior la deja huerfana sin
romper nada (ningun modelo viejo la mira), asi que no hace falta un downgrade en
ningun escenario de rollback. Sin backfill: no hay nada que migrar.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_reactivation_tokens"
down_revision = "0016_user_deactivated_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reactivation_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_reactivation_tokens_user_id", "reactivation_tokens", ["user_id"])
    op.create_index(
        "ix_reactivation_tokens_token_hash", "reactivation_tokens", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_table("reactivation_tokens")

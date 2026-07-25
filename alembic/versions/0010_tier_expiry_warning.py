"""tier expiry warning flag

Revision ID: 0010_tier_expiry_warning
Revises: 0009_pending_registrations
Create Date: 2026-07-25
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_tier_expiry_warning"
down_revision = "0009_pending_registrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Marca del aviso "vence pronto" (idempotencia del job diario). Nullable: NULL =
    # aun no se aviso en el periodo vigente. Se limpia al renovar/cambiar tier.
    op.add_column(
        "user_tiers",
        sa.Column("expiry_warning_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_tiers", "expiry_warning_sent_at")

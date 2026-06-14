"""user tiers

Revision ID: 0004_user_tiers
Revises: 0003_export_windows
Create Date: 2026-06-14
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_user_tiers"
down_revision = "0003_export_windows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_tiers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tier", sa.String(16), server_default="free", nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_user_tiers_user_id", "user_tiers", ["user_id"], unique=True)


def downgrade() -> None:
    op.drop_table("user_tiers")

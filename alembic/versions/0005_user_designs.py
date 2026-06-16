"""user designs

Revision ID: 0005_user_designs
Revises: 0004_user_tiers
Create Date: 2026-06-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_user_designs"
down_revision = "0004_user_tiers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_designs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("json_key", sa.String(512), nullable=False),
        sa.Column("thumb_key", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # NO unique: un usuario tiene varios disenos.
    op.create_index("ix_user_designs_user_id", "user_designs", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_table("user_designs")

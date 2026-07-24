"""pending registrations (double opt-in signup)

Revision ID: 0009_pending_registrations
Revises: 0008_login_otps
Create Date: 2026-07-24
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_pending_registrations"
down_revision = "0008_login_otps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_registrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pending_registrations_email", "pending_registrations", ["email"])
    op.create_index(
        "ix_pending_registrations_token_hash", "pending_registrations", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_table("pending_registrations")

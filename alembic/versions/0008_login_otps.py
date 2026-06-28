"""login otps

Revision ID: 0008_login_otps
Revises: 0007_google_oauth
Create Date: 2026-06-27

Codigo OTP de un solo uso para el 2do factor del login por email. Mismo patron
que `password_reset_tokens`: en DB vive SOLO el HMAC del codigo (`code_hash`),
single-use (`used_at`), de vida corta (`expires_at`) y con tope de `attempts`.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_login_otps"
down_revision = "0007_google_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_otps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_login_otps_user_id", "login_otps", ["user_id"])
    op.create_index("ix_login_otps_code_hash", "login_otps", ["code_hash"], unique=True)


def downgrade() -> None:
    op.drop_table("login_otps")

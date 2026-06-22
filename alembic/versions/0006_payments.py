"""payments

Revision ID: 0006_payments
Revises: 0005_user_designs
Create Date: 2026-06-19
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_payments"
down_revision = "0005_user_designs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan", sa.String(16), nullable=False),
        sa.Column("mp_payment_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_payments_user_id", "payments", ["user_id"], unique=False)
    # UNIQUE: idempotencia del webhook (MP reintenta; un pago se procesa una vez).
    op.create_index("ix_payments_mp_payment_id", "payments", ["mp_payment_id"], unique=True)


def downgrade() -> None:
    op.drop_table("payments")

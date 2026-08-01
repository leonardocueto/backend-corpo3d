"""subscriptions and export logs

Revision ID: 0011_subs_export_logs
Revises: 0010_tier_expiry_warning
Create Date: 2026-08-01
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_subs_export_logs"
down_revision = "0010_tier_expiry_warning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("plan", sa.String(16), nullable=False),
        sa.Column("mp_preapproval_id", sa.String(128), nullable=False, unique=True, index=True),
        sa.Column("mp_status", sa.String(16), nullable=False),
        sa.Column("mp_payer_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "export_logs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "payment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payments.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("r2_key", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
    )

    op.add_column(
        "payments",
        sa.Column(
            "subscription_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

    op.add_column(
        "user_tiers",
        sa.Column(
            "subscription_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("user_tiers", "subscription_id")
    op.drop_column("payments", "subscription_id")
    op.drop_table("export_logs")
    op.drop_table("subscriptions")

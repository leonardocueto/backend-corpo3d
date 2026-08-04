"""user FK set null on payments and export_logs

Revision ID: 0012_user_fk_set_null
Revises: 0011_subs_export_logs
Create Date: 2026-08-03
"""
from alembic import op

revision = "0012_user_fk_set_null"
down_revision = "0011_subs_export_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("payments_user_id_fkey", "payments", type_="foreignkey")
    op.alter_column("payments", "user_id", nullable=True)
    op.create_foreign_key(
        "payments_user_id_fkey",
        "payments",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_constraint("export_logs_user_id_fkey", "export_logs", type_="foreignkey")
    op.alter_column("export_logs", "user_id", nullable=True)
    op.create_foreign_key(
        "export_logs_user_id_fkey",
        "export_logs",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    for table in ("payments", "export_logs"):
        op.drop_constraint(f"{table}_user_id_fkey", table, type_="foreignkey")
        op.alter_column(table, "user_id", nullable=False)
        op.create_foreign_key(
            f"{table}_user_id_fkey",
            table,
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )

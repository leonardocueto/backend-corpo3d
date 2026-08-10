"""force password change on admin-issued temporary passwords

Revision ID: 0013_must_change_password
Revises: 0012_user_fk_set_null
Create Date: 2026-08-10
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_must_change_password"
down_revision = "0012_user_fk_set_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default="false" para que las filas existentes queden en false sin
    # tener que backfillear a mano (y para que el NOT NULL no rompa el ALTER).
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "must_change_password")

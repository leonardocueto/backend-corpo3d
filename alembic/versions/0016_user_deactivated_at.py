"""fecha de baja de la cuenta (self-service)

Acompana a `POST /auth/deactivate`. `users.is_active` ya existia y es lo que corta
el acceso (lo valida `deps.py` en cada request), pero es un bool: no dice CUANDO se
pidio la baja. Sin esa fecha no se puede sostener el plazo del art. 16 de la Ley
25.326 (5 dias habiles para suprimir) ni saber a que cuentas les toca el borrado o
anonimizado posterior.

Sin backfill: hasta ahora no habia baja self-service, asi que ninguna cuenta inactiva
proviene de un pedido del usuario.

Revision ID: 0016_user_deactivated_at
Revises: 0015_withdrawal_requests
Create Date: 2026-08-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0016_user_deactivated_at"
down_revision = "0015_withdrawal_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "deactivated_at")

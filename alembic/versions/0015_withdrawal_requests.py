"""withdrawal requests (arrepentimiento, Ley 24.240 art. 34)

Persiste la solicitud de arrepentimiento, que hasta ahora solo existia como un
mail. `POST /payments/withdrawal` encolaba el envio y respondia 204 sin escribir
nada; como el helper `_send` traga los errores por diseno, una caida de Resend
dejaba al cliente con un exito falso y la solicitud sin rastro. Esta fila pasa a
ser el registro legal y el mail queda como notificacion.

Sin backfill: las solicitudes anteriores no quedaron guardadas en ningun lado y
no hay de donde reconstruirlas.

Revision ID: 0015_withdrawal_requests
Revises: 0014_legal_acceptance
Create Date: 2026-08-14
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_withdrawal_requests"
down_revision = "0014_legal_acceptance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "withdrawal_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("full_name", sa.String(255), nullable=False),
        # Sin FK a users: el endpoint es publico y quien revoca puede no tener sesion.
        sa.Column("email", sa.String(255), nullable=False),
        # Nullable: el art. 34 permite revocar SIN justificar.
        sa.Column("reason", sa.String(2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_withdrawal_requests_email", "withdrawal_requests", ["email"])
    # created_at indexado: el panel ordena por fecha y filtra las que superan los 10 dias.
    op.create_index("ix_withdrawal_requests_created_at", "withdrawal_requests", ["created_at"])


def downgrade() -> None:
    op.drop_table("withdrawal_requests")

"""persist terms and privacy acceptance on signup

Revision ID: 0014_terms_acceptance
Revises: 0013_must_change_password
Create Date: 2026-08-13
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_terms_acceptance"
down_revision = "0013_must_change_password"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable a proposito: NULL = "no consta aceptacion" (altas hechas por admin,
    # donde no hay nadie del otro lado que acepte nada). No hay server_default: es
    # un timestamp de evento, se setea desde Python.
    op.add_column("users", sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("terms_version", sa.String(32), nullable=True))

    # Las mismas dos en el pending: la aceptacion ocurre en el signup pero el User
    # recien nace al confirmar el mail, asi que el dato tiene que viajar por aca
    # (mismo camino que `password_hash`).
    op.add_column(
        "pending_registrations",
        sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("pending_registrations", sa.Column("terms_version", sa.String(32), nullable=True))

    # Usuarios previos al checkbox: se los da por aceptados (pidieron acceso antes
    # de que existiera el flujo). Se marca 'legacy-backfill', NO la version vigente:
    # es una aceptacion asumida por uso previo, no un clic registrado. Un registro
    # que afirmara lo contrario seria peor evidencia que ninguno.
    op.execute(
        "UPDATE users SET terms_accepted_at = created_at, terms_version = 'legacy-backfill' "
        "WHERE terms_accepted_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("pending_registrations", "terms_version")
    op.drop_column("pending_registrations", "terms_accepted_at")
    op.drop_column("users", "terms_version")
    op.drop_column("users", "terms_accepted_at")

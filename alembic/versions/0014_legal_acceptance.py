"""persist acceptance of terms and privacy, one version per document

Revision ID: 0014_legal_acceptance
Revises: 0013_must_change_password
Create Date: 2026-08-13
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_legal_acceptance"
down_revision = "0013_must_change_password"
branch_labels = None
depends_on = None

# Una columna de fecha + una de version POR DOCUMENTO. Se guarda la version (no un
# bool) porque es lo que permite pedir la re-aceptacion cuando el documento cambia:
# el estado "acepto" se deriva comparando contra la version vigente.
_COLUMNS = (
    ("terms_accepted_at", "terms_version"),
    ("privacy_accepted_at", "privacy_version"),
)


def upgrade() -> None:
    # Nullable a proposito: NULL = "no consta aceptacion" (altas hechas por admin,
    # donde no hay nadie del otro lado que acepte nada). Sin server_default: son
    # timestamps de evento, se setean desde Python.
    for table in ("users", "pending_registrations"):
        for at_col, version_col in _COLUMNS:
            op.add_column(table, sa.Column(at_col, sa.DateTime(timezone=True), nullable=True))
            op.add_column(table, sa.Column(version_col, sa.String(32), nullable=True))

    # Usuarios previos al checkbox: se los da por aceptados (pidieron acceso antes
    # de que existiera el flujo). Se marca 'legacy-backfill' y NO la version vigente,
    # por dos motivos: (1) es una aceptacion asumida por uso previo, no un clic
    # registrado, y un registro que afirmara lo contrario seria peor evidencia que
    # ninguno; (2) al no coincidir con la version vigente, el estado derivado da
    # False y la app les pide aceptar de verdad en /politicas.
    op.execute(
        "UPDATE users SET "
        "terms_accepted_at = created_at, terms_version = 'legacy-backfill', "
        "privacy_accepted_at = created_at, privacy_version = 'legacy-backfill' "
        "WHERE terms_accepted_at IS NULL"
    )


def downgrade() -> None:
    for table in ("pending_registrations", "users"):
        for at_col, version_col in _COLUMNS:
            op.drop_column(table, version_col)
            op.drop_column(table, at_col)

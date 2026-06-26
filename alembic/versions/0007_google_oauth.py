"""google oauth

Revision ID: 0007_google_oauth
Revises: 0006_payments
Create Date: 2026-06-26

Login con Google (OIDC). El usuario sigue viviendo en `users`; Google solo
verifica identidad. Cambios en `users`:
- `password_hash` -> nullable (un usuario de Google no tiene password).
- `google_sub` (unique) -> `sub` estable de la cuenta de Google (linkeo robusto).
- `auth_provider` -> 'password' | 'google' (informativo para el panel admin).

OJO numeracion: el plan original (OAuth.md) asumia 0006, pero 0006 ya lo tomo
`payments`, asi que esta migracion es 0007 colgando de 0006_payments.
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_google_oauth"
down_revision = "0006_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("users", "password_hash", existing_type=sa.String(255), nullable=True)
    op.add_column("users", sa.Column("google_sub", sa.String(255), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "auth_provider",
            sa.String(16),
            nullable=False,
            server_default="password",  # filas existentes quedan como 'password'
        ),
    )
    op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_google_sub", table_name="users")
    op.drop_column("users", "auth_provider")
    op.drop_column("users", "google_sub")
    # Revertir a NOT NULL solo es seguro si no hay usuarios sin password (Google).
    op.alter_column("users", "password_hash", existing_type=sa.String(255), nullable=False)

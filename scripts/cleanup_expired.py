"""Limpieza de filas vencidas de tablas efimeras. Uso: python -m scripts.cleanup_expired

Pensado para correr UNA VEZ POR DIA (Render Cron Job o manual). Borra filas con
`expires_at < now()` de: sessions (tambien revocadas), password_reset_tokens
y pending_registrations. No toca users, payments ni tiers.
login_otps ya no se limpia aca: los OTPs viven en Redis con TTL nativo.

`--dry-run`: muestra cuantas filas se borrarian SIN borrar."""
import argparse
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from app.database import SessionLocal
from app.models import PasswordResetToken, PendingRegistration, Session

TABLES = [
    ("sessions", Session, Session.expires_at),
    ("password_reset_tokens", PasswordResetToken, PasswordResetToken.expires_at),
    ("pending_registrations", PendingRegistration, PendingRegistration.expires_at),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Cleanup de filas vencidas.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Muestra cuantas filas se borrarian sin eliminar nada.",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        for name, model, expires_col in TABLES:
            if args.dry_run:
                count = db.execute(
                    select(func.count()).where(expires_col < now)
                ).scalar_one()
                print(f"[dry-run] {name}: {count} fila(s) vencidas")
            else:
                result = db.execute(delete(model).where(expires_col < now))
                db.commit()
                print(f"{name}: {result.rowcount} fila(s) eliminadas")
    finally:
        db.close()


if __name__ == "__main__":
    main()

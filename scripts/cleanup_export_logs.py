"""Limpieza de logs de auditoria de exportaciones. Uso: python -m scripts.cleanup_export_logs

Pensado para correr UNA VEZ POR DIA (Render Cron Job). Borra los ExportLog (DB)
y sus archivos .txt (R2) que superan `export_log_retention_days` (default 180 =
6 meses). Los Payment de la DB NO se tocan (quedan para siempre en admin billing).

`--dry-run`: lista lo que se borraria SIN borrar."""
import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import settings
from app.database import SessionLocal
from app.models import ExportLog
from app.storage import delete_export_logs

CHUNK = 500


def main() -> None:
    parser = argparse.ArgumentParser(description="Cleanup de export logs viejos.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lista los logs a borrar sin eliminar nada.",
    )
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.export_log_retention_days)

    db = SessionLocal()
    total_deleted = 0
    try:
        while True:
            rows = db.execute(
                select(ExportLog.id, ExportLog.r2_key)
                .where(ExportLog.created_at < cutoff)
                .limit(CHUNK)
            ).all()

            if not rows:
                break

            ids = [r.id for r in rows]
            keys = [r.r2_key for r in rows]

            if args.dry_run:
                for r in rows:
                    print(f"[dry-run] borraria {r.r2_key}")
                total_deleted += len(rows)
                break

            delete_export_logs(keys)
            db.execute(delete(ExportLog).where(ExportLog.id.in_(ids)))
            db.commit()
            total_deleted += len(rows)
    finally:
        db.close()

    if args.dry_run:
        print(f"[dry-run] {total_deleted} log(s) se borrarian.")
    else:
        print(f"Logs eliminados: {total_deleted}")


if __name__ == "__main__":
    main()

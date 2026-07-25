"""Aviso de vencimiento proximo de tiers pagos. Uso: python -m scripts.notify_expiring

Pensado para correr UNA VEZ POR DIA (Render Cron Job). Busca los tiers pagos que
vencen dentro de `TIER_EXPIRY_WARNING_DAYS` (default 10) y a los que todavia NO se les
aviso (`expiry_warning_sent_at IS NULL`), les manda el mail de "vence pronto" y estampa
la marca (idempotencia: no reenvia al dia siguiente). Al renovar/pagar o cambiar el
tier, la marca se limpia (ver tiers.activate_paid_tier / set_user_tier), asi el nuevo
periodo vuelve a avisar.

`--dry-run`: lista a quien se le avisaria SIN enviar ni estampar (para verificar)."""
import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.email import send_tier_expiring_email
from app.models import User, UserTier
from app.routers.tiers import PAID_TIERS


def main() -> None:
    parser = argparse.ArgumentParser(description="Aviso de vencimiento de tiers pagos.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lista los avisos sin enviar mails ni estampar la marca.",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=settings.tier_expiry_warning_days)
    renew_link = f"{settings.frontend_url.rstrip('/')}/pricing"

    db = SessionLocal()
    sent = 0
    try:
        # Tier pago, vigente (aun no vencido), dentro de la ventana de aviso y sin
        # aviso previo en este periodo. Solo usuarios activos.
        rows = db.execute(
            select(UserTier, User)
            .join(User, User.id == UserTier.user_id)
            .where(
                UserTier.tier.in_(PAID_TIERS),
                UserTier.expires_at.is_not(None),
                UserTier.expires_at > now,
                UserTier.expires_at <= window_end,
                UserTier.expiry_warning_sent_at.is_(None),
                User.is_active.is_(True),
            )
        ).all()

        for tier, user in rows:
            plan_label = "Mensual" if tier.tier == "mensual" else "Anual"
            expires_str = tier.expires_at.strftime("%d/%m/%Y")
            if args.dry_run:
                print(f"[dry-run] avisaria a {user.email} ({plan_label}, vence {expires_str})")
                continue
            send_tier_expiring_email(user.email, plan_label, expires_str, renew_link)
            tier.expiry_warning_sent_at = now
            sent += 1

        if not args.dry_run:
            db.commit()
    finally:
        db.close()

    if args.dry_run:
        print(f"[dry-run] {len(rows)} tier(s) entrarian en el aviso.")
    else:
        print(f"Avisos enviados: {sent}")


if __name__ == "__main__":
    main()

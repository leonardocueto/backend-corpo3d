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

from sqlalchemy import exists, select

from app.config import settings
from app.database import SessionLocal
from app.email import send_tier_expiring_email
from app.mailing.render import format_date
from app.models import Subscription, User, UserTier
from app.routers.tiers import PAID_TIERS, plan_copy


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
        has_active_sub = exists(
            select(Subscription.id).where(
                Subscription.user_id == UserTier.user_id,
                Subscription.mp_status == "authorized",
            )
        )
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
                ~has_active_sub,
            )
        ).all()

        for tier, user in rows:
            plan_label = plan_copy(tier.tier).label
            expires_str = format_date(tier.expires_at)
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

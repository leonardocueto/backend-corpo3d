"""Dias de gracia (`TIER_GRACE`) segun el estado de la suscripcion.

Con la suscripcion `cancelled` el plan vence en `expires_at` exacto: el colchon
existe para esperar el webhook de una renovacion, y una cancelada no renueva.
Cualquier otro estado (o un plan sin suscripcion) conserva los 3 dias.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.routers.tiers import TIER_GRACE, tier_is_unlimited, user_is_unlimited

NOW = datetime(2026, 12, 12, 12, 0, tzinfo=UTC)
# Vencio hace un dia: dentro de los 3 de gracia.
EXPIRED_YESTERDAY = NOW - timedelta(days=1)


def _setup(db_session, user, mp_status: str | None):
    from app.models import Subscription, UserTier

    sub = None
    if mp_status is not None:
        sub = Subscription(
            user_id=user.id,
            plan="mensual",
            mp_preapproval_id=uuid.uuid4().hex,
            mp_status=mp_status,
        )
        db_session.add(sub)
        db_session.flush()
    tier = UserTier(
        user_id=user.id,
        tier="mensual",
        paid_at=EXPIRED_YESTERDAY - timedelta(days=30),
        expires_at=EXPIRED_YESTERDAY,
        subscription_id=sub.id if sub else None,
    )
    db_session.add(tier)
    db_session.commit()
    return tier


def test_pure_function_grace_only_when_not_cancelled():
    from app.models import UserTier

    tier = UserTier(tier="mensual", expires_at=EXPIRED_YESTERDAY)
    assert tier_is_unlimited(tier, NOW) is True
    assert tier_is_unlimited(tier, NOW, cancelled=True) is False
    # Antes del vencimiento, cancelada o no, sigue vigente.
    assert tier_is_unlimited(tier, EXPIRED_YESTERDAY - timedelta(seconds=1), cancelled=True)
    # Pasada la gracia, vencido en los dos casos.
    assert tier_is_unlimited(tier, EXPIRED_YESTERDAY + TIER_GRACE) is False


def test_cancelled_subscription_expires_without_grace(db_session, make_user):
    user = make_user()
    tier = _setup(db_session, user, "cancelled")
    assert user_is_unlimited(db_session, user, NOW) is False
    db_session.refresh(tier)
    assert tier.tier == "free"


@pytest.mark.parametrize("mp_status", [None, "paused"])
def test_grace_kept_without_cancelled_subscription(db_session, make_user, mp_status):
    user = make_user()
    tier = _setup(db_session, user, mp_status)
    assert user_is_unlimited(db_session, user, NOW) is True
    db_session.refresh(tier)
    assert tier.tier == "mensual"

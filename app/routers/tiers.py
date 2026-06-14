import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.database import get_db
from app.deps import require_admin
from app.models import User, UserTier
from app.schemas import SetTierIn, UserTierOut

# Tiers de usuario. `free` = limite normal (3 intentos / 24h); `mensual`/`anual`
# = exportaciones ilimitadas mientras el pago no haya vencido. El vencimiento se
# evalua al leer (hora del servidor); un pago vencido revierte a free solo.
router = APIRouter(prefix="/tiers", tags=["tiers"])

PAID_TIERS = {"mensual", "anual"}
# Duracion de cada tier pago (timedelta, sin dependencia extra; si se quiere
# mes/año calendario exacto, usar python-dateutil relativedelta).
TIER_DURATION = {"mensual": timedelta(days=30), "anual": timedelta(days=365)}


def tier_is_unlimited(tier: "UserTier | None", now: datetime) -> bool:
    """True si el tier es pago y NO esta vencido. Funcion pura (sirve para el
    batch del listado admin)."""
    return (
        tier is not None
        and tier.tier in PAID_TIERS
        and tier.expires_at is not None
        and tier.expires_at > now
    )


def user_is_unlimited(db: DbSession, user: User, now: datetime) -> bool:
    """True si el usuario no tiene limite de exportaciones: admin, o tier pago
    vigente. Fuente unica de verdad del concepto 'ilimitado'."""
    if user.is_admin:
        return True
    tier = db.scalar(select(UserTier).where(UserTier.user_id == user.id))
    return tier_is_unlimited(tier, now)


def expiry_for(tier_name: str, now: datetime) -> datetime:
    return now + TIER_DURATION[tier_name]


def _get_or_create_locked(db: DbSession, user_id: uuid.UUID) -> UserTier:
    """Fila del usuario con lock de escritura; la crea si no existe (con la misma
    proteccion ante carrera que exports.set_user_attempts)."""
    tier = db.scalar(
        select(UserTier).where(UserTier.user_id == user_id).with_for_update()
    )
    if tier is not None:
        return tier
    tier = UserTier(user_id=user_id, tier="free")
    db.add(tier)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        tier = db.scalar(
            select(UserTier).where(UserTier.user_id == user_id).with_for_update()
        )
    return tier


@router.put(
    "/{user_id}",
    response_model=UserTierOut,
    dependencies=[Depends(require_admin)],
)
def set_user_tier(
    user_id: uuid.UUID, payload: SetTierIn, db: DbSession = Depends(get_db)
) -> UserTierOut:
    """Asigna el tier de un usuario (admin). `free` limpia el pago; `mensual`/
    `anual` setean paid_at=ahora y expires_at=ahora+periodo. A un admin no se le
    asigna tier (es ilimitado por rol) -> 400."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")
    if target.is_admin:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="No se puede asignar tier a un admin (es ilimitado)",
        )

    now = datetime.now(timezone.utc)
    tier = _get_or_create_locked(db, user_id)
    if payload.tier == "free":
        tier.tier = "free"
        tier.paid_at = None
        tier.expires_at = None
    else:
        tier.tier = payload.tier
        tier.paid_at = now
        tier.expires_at = expiry_for(payload.tier, now)

    db.commit()
    db.refresh(tier)
    return UserTierOut(tier=tier.tier, paid_at=tier.paid_at, expires_at=tier.expires_at)

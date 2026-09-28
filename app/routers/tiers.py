import calendar
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.database import get_db
from app.deps import require_admin, require_legal_acceptance
from app.models import Subscription, User, UserTier
from app.ratelimit import limiter
from app.schemas import MyTierOut, SetTierIn, UserTierOut

# Tiers de usuario. Autoridad dual: suscripcion MP activa (primario) +
# `expires_at` como safety net (fallback para legacy one-time y webhook que
# no llega). `free` = limite normal (3 intentos / 24h); `mensual`/`anual`
# = exportaciones ilimitadas.
router = APIRouter(prefix="/tiers", tags=["tiers"])

PAID_TIERS = {"mensual", "anual"}
# Duracion de cada tier pago, en MESES CALENDARIO. Antes eran timedelta(days=30)
# y timedelta(days=365), y eso abria un agujero todos los meses: la preapproval de
# MP se crea con `frequency: 1, frequency_type: "months"`, asi que MP debita el
# MISMO DIA de cada mes (28 a 31 dias), mientras el tier vencia a los 30 fijos. Un
# usuario que se suscribio un 28 de agosto se quedaba sin plan el 27 de septiembre
# y MP le cobraba el 28: un dia de corte, dos en febrero, y el desfasaje se corria
# mes a mes. Se resuelve contando meses calendario (ver `_add_months`).
TIER_MONTHS = {"mensual": 1, "anual": 12}

# Colchon entre el fin del periodo pago y el corte efectivo de acceso.
#
# Se aplica al CHEQUEO (`tier_is_unlimited`), NO a `expires_at`. Dos razones:
# la fila sigue diciendo hasta cuando se pago —que es lo que muestra la UI y lo
# que usa el aviso de vencimiento—, y al renovar contra `expires_at` el colchon no
# se acumula periodo a periodo (si estuviera sumado en la columna, cada renovacion
# arrastraria los dias de la anterior).
#
# Cubre la latencia normal entre el vencimiento y la llegada del webhook del cobro.
# NO pretende cubrir el ciclo completo de reintentos de MP (hasta 4 intentos en una
# ventana de ~10 dias, y la suscripcion se cancela sola tras 3 cobros fallidos
# consecutivos): si el cobro falla de verdad, el acceso tiene que cortarse.
TIER_GRACE = timedelta(days=3)

# Hasta cuando se sigue confiando en una suscripcion `authorized` de MP despues de
# que vencio el periodo pago.
#
# `user_is_unlimited` da acceso con solo ver la suscripcion `authorized`, sin mirar
# si el ultimo cobro entro — y MP la deja `authorized` mucho despues de que los
# cobros empiezan a fallar: hasta 4 reintentos en ~10 dias por ciclo, y recien tras
# 3 ciclos consecutivos fallidos la cancela sola. Sin este tope, alguien podia dejar
# de pagar y conservar las exportaciones ilimitadas unos 3 meses.
#
# 15 dias cubre con margen la ventana de reintentos, asi que a un cobro que se
# recupera al 3er intento no se le corta nada; solo pierde el acceso quien
# efectivamente dejo de pagar. Es mas largo que TIER_GRACE a proposito: aquel cubre
# la latencia de un webhook, este cubre el ciclo de cobranza de MP.
SUBSCRIPTION_DUNNING = timedelta(days=15)

# Limite por IP de GET /me. Lo llama el navbar una vez por carga de pagina, asi
# que 30/min deja lugar de sobra para navegar y acota un loop accidental.
ME_LIMIT = "30/minute"


class PlanCopy(NamedTuple):
    """Nombres visibles de un plan. Vive aca (y no en el router de pagos) porque el
    vocabulario de planes es de este modulo: lo usan el payload de MP, los mails
    transaccionales y el cron de vencimientos."""

    label: str  # "Mensual" / "Anual"
    period_every: str  # "todos los meses" / "todos los años"
    period_each: str  # "cada mes" / "cada año"


_PLAN_COPY = {
    "mensual": PlanCopy("Mensual", "todos los meses", "cada mes"),
    "anual": PlanCopy("Anual", "todos los años", "cada año"),
}


def plan_copy(plan: str | None) -> PlanCopy:
    """Copy visible del plan; fallback a anual si es desconocido (mismo default que
    tenian los ternarios que reemplaza)."""
    return _PLAN_COPY.get(plan or "", _PLAN_COPY["anual"])


def tier_is_unlimited(tier: "UserTier | None", now: datetime) -> bool:
    """True si el tier es pago y NO esta vencido. Funcion pura (sirve para el
    batch del listado admin).

    El corte es `expires_at + TIER_GRACE`, no `expires_at` pelado: el colchon vive
    aca y no en la columna (ver TIER_GRACE)."""
    return (
        tier is not None
        and tier.tier in PAID_TIERS
        and tier.expires_at is not None
        and tier.expires_at + TIER_GRACE > now
    )


def downgrade_if_expired(tier: "UserTier | None", now: datetime) -> bool:
    """Si el tier es pago y vencio, lo pasa FISICAMENTE a 'free' (mantiene
    paid_at/expires_at como historial del ultimo pago). Devuelve True si cambio
    algo (para que el caller commitee). Idempotente: ya en 'free' no toca nada."""
    if tier is not None and tier.tier in PAID_TIERS and not tier_is_unlimited(tier, now):
        tier.tier = "free"
        return True
    return False


def sync_user_tier(db: DbSession, user: User, now: datetime) -> "UserTier | None":
    """Reconcilia el tier del usuario: si su tier pago vencio, lo degrada a 'free'
    y persiste. Reusable (login + user_is_unlimited). No-op si no hay fila."""
    tier = db.scalar(select(UserTier).where(UserTier.user_id == user.id))
    if downgrade_if_expired(tier, now):
        db.commit()
    return tier


def user_is_unlimited(db: DbSession, user: User, now: datetime) -> bool:
    """True si el usuario no tiene limite de exportaciones: admin, suscripcion
    activa (primario), o tier pago vigente por expires_at (fallback legacy +
    safety net). Fuente unica de verdad del concepto 'ilimitado'.

    La rama de la suscripcion **caduca a los `SUBSCRIPTION_DUNNING` dias** del
    vencimiento del periodo pago. Existe para que un webhook que no llega no le corte
    el acceso a alguien que si pago, pero sin tope protegia tambien al que dejo de
    pagar: MP no cancela la suscripcion hasta 3 ciclos fallidos, o sea unos 3 meses
    de acceso gratis."""
    if user.is_admin:
        return True
    tier = sync_user_tier(db, user, now)
    if tier and tier.subscription_id and _within_dunning(tier, now):
        sub = db.get(Subscription, tier.subscription_id)
        if sub and sub.mp_status == "authorized":
            return True
    return tier_is_unlimited(tier, now)


def _within_dunning(tier: UserTier, now: datetime) -> bool:
    """True si el periodo pago del tier no venció hace más de `SUBSCRIPTION_DUNNING`.

    Sin `expires_at` devuelve True: no hay contra qué medir el atraso, y ante la duda
    no se le corta el acceso a alguien con una suscripción activa."""
    if tier.expires_at is None:
        return True
    return tier.expires_at + SUBSCRIPTION_DUNNING > now


def _add_months(dt: datetime, months: int) -> datetime:
    """Suma meses CALENDARIO conservando el dia del mes. Si el dia no existe en el
    mes destino se cae al ultimo dia (31-ene + 1 mes -> 28/29-feb), que es el mismo
    criterio que usa MP para agendar el debito siguiente.

    A mano y no con `dateutil.relativedelta` para no sumar una dependencia por seis
    lineas; `calendar.monthrange` ya resuelve los años bisiestos."""
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def expiry_for(tier_name: str, now: datetime) -> datetime:
    """Fin del periodo pago: un mes/año CALENDARIO (no 30/365 dias), contado SIEMPRE
    desde `now`.

    Contar desde `now` y no desde el `expires_at` vigente es deliberado, y costó un
    bug. Extender desde el vencimiento parece mejor —"no se pierden los días que
    sobraban"— pero es innecesario: MP cobra EL DÍA del aniversario, así que contar
    desde el cobro ya deja el vencimiento clavado en esa fecha. La deriva que se
    quería evitar venía de `timedelta(days=30)`, que no es un mes; con meses
    calendario no existe.

    Y extender ACUMULA. En un alta nueva, MP manda el evento `authorized` de la
    preapproval **y** la factura del primer cobro: el primero ponía un mes y el
    segundo estiraba sobre ese, con lo cual un suscriptor nuevo se llevaba DOS meses
    por un solo pago. Contando desde `now` los dos eventos calculan el mismo valor y
    la operación queda idempotente."""
    return _add_months(now, TIER_MONTHS[tier_name])


def activate_subscription_tier(
    db: DbSession,
    user_id: uuid.UUID,
    plan: str,
    subscription_id: uuid.UUID,
    now: datetime,
) -> UserTier:
    """Activa **o renueva** un tier desde una suscripcion. Como activate_paid_tier
    pero ademas linkea la suscripcion.

    Alta y renovacion son la MISMA operacion, y conviene que siga siendo asi: como
    `expiry_for` cuenta desde `now`, aplicarla dos veces por el mismo cobro da el
    mismo resultado. Eso es lo que vuelve inofensivo que MP notifique un alta por dos
    eventos (`authorized` de la preapproval + la factura del primer cobro).

    Reescribe `tier` y `subscription_id` enteros, no solo `expires_at`. En la
    renovacion eso no es cosmetico: si el downgrade perezoso
    (`sync_user_tier` -> `downgrade_if_expired`) ya paso la fila a 'free', tocar solo
    `expires_at` la dejaria en 'free' con vencimiento futuro — un estado que
    `tier_is_unlimited` lee como vencido y del que nada la saca."""
    tier = _get_or_create_locked(db, user_id)
    tier.tier = plan
    tier.paid_at = now
    tier.expires_at = expiry_for(plan, now)
    tier.subscription_id = subscription_id
    tier.expiry_warning_sent_at = None
    return tier


def deactivate_subscription_tier(
    db: DbSession, user_id: uuid.UUID,
) -> "UserTier | None":
    """Revierte el tier a free cuando la suscripcion se pausa/cancela. Mantiene
    paid_at/expires_at como historial."""
    tier = db.scalar(
        select(UserTier).where(UserTier.user_id == user_id).with_for_update()
    )
    if tier is None:
        return None
    tier.tier = "free"
    tier.subscription_id = None
    tier.expiry_warning_sent_at = None
    return tier


def activate_paid_tier(
    db: DbSession, user_id: uuid.UUID, plan: str, now: datetime
) -> UserTier:
    """Activa (o renueva) un tier pago: get-or-create con lock, setea
    tier/paid_at/expires_at desde `now`. NO commitea (lo hace el caller, asi el
    webhook activa el tier y registra el Payment en UNA sola transaccion). Lo usan
    el endpoint admin (set_user_tier) y el webhook de Mercado Pago.

    Cuenta desde `now`, y eso es deliberado: aca caen el alta manual de un admin
    ("dale un mes desde hoy") y el pago unico legacy, donde no hay un periodo previo
    al que engancharse. La renovacion de una suscripcion NO usa esta funcion, usa
    `renew_subscription_tier`, que extiende desde el `expires_at` vigente."""
    tier = _get_or_create_locked(db, user_id)
    tier.tier = plan
    tier.paid_at = now
    tier.expires_at = expiry_for(plan, now)
    # Nuevo periodo: rearmamos el aviso de vencimiento (el job volvera a avisar a los
    # ~10 dias del nuevo expires_at).
    tier.expiry_warning_sent_at = None
    return tier


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


@router.get("/me", response_model=MyTierOut)
@limiter.limit(ME_LIMIT)
def my_tier(
    request: Request,
    user: User = Depends(require_legal_acceptance),
    db: DbSession = Depends(get_db),
) -> MyTierOut:
    """Estado de plan del usuario logueado. Lo consume el aviso de "plan por vencer"
    del front (modal + banner a <=5 dias del vencimiento).

    Va aca y no en `UserOut` a proposito: `/auth/me` corre en CADA arranque de la
    SPA para todos los usuarios, y este endpoint llama a `sync_user_tier`, que
    puede COMMITEAR (el downgrade de un tier vencido es perezoso, on read). Meter
    eso en el camino critico del boot, para un cartel que ve una minoria, no vale
    la pena; ademas `UserOut` se mantiene minimo a proposito (ver backend/CLAUDE.md).

    Tampoco sirve `GET /payments/subscription`: filtra las suscripciones
    `cancelled` y devuelve None, que es justo el caso de la mayoria de la audiencia
    de este aviso.

    `sync_user_tier` NO es opcional: sin el, un plan que vencio anoche seguiria
    reportando tier='mensual' con un expires_at pasado, y el front calcularia dias
    negativos.

    `auto_renew` usa el MISMO criterio que scripts/notify_expiring.py (cualquier
    Subscription 'authorized' del usuario, no solo la linkeada en
    tier.subscription_id): a quien tiene debito automatico no hay que avisarle nada,
    porque MP le cobra solo. Si los dos criterios divergieran, un usuario recibiria
    el mail y no el cartel, o al reves."""
    now = datetime.now(timezone.utc)
    tier = sync_user_tier(db, user, now)
    auto_renew = bool(
        db.scalar(
            select(
                exists().where(
                    Subscription.user_id == user.id,
                    Subscription.mp_status == "authorized",
                )
            )
        )
    )
    return MyTierOut(
        tier=tier.tier if tier else "free",
        expires_at=tier.expires_at if tier else None,
        auto_renew=auto_renew,
    )


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
    if payload.tier == "free":
        tier = _get_or_create_locked(db, user_id)
        tier.tier = "free"
        tier.paid_at = None
        tier.expires_at = None
        tier.expiry_warning_sent_at = None
    else:
        tier = activate_paid_tier(db, user_id, payload.tier, now)

    db.commit()
    db.refresh(tier)
    return UserTierOut(tier=tier.tier, paid_at=tier.paid_at, expires_at=tier.expires_at)

import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

import mercadopago
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.config import settings
from app.database import get_db
from app.deps import require_admin, require_legal_acceptance
from app.email import (
    send_payment_approved_email,
    send_payment_rejected_email,
    send_refund_email,
    send_subscription_activated_email,
    send_subscription_cancelled_email,
    send_subscription_charge_failed_email,
    send_subscription_paused_email,
    send_withdrawal_request_email,
)
from app.mailing.render import format_amount, format_date
from app.models import Payment, Subscription, User, UserTier
from app.ratelimit import limiter
from app.routers.tiers import (
    PAID_TIERS,
    activate_paid_tier,
    activate_subscription_tier,
    deactivate_subscription_tier,
    expiry_for,
    plan_copy,
)
from app.schemas import (
    CancelSubscriptionOut,
    PaymentOut,
    PaymentsPage,
    SubscribeIn,
    SubscribeOut,
    SubscriptionOut,
    WithdrawalRequestIn,
)

router = APIRouter(prefix="/payments", tags=["payments"])

logger = logging.getLogger("payments")

LIST_LIMIT = "60/minute"

_sdk: "mercadopago.SDK | None" = None

_FREQUENCY = {"mensual": (1, "months"), "anual": (12, "months")}

# Estados de MP en los que la plata VOLVIO al pagador. Disparan la revocacion
# inmediata del tier (`_apply_refund`), a diferencia de cancelar/pausar, que solo
# corta cobros futuros y conserva el acceso hasta `expires_at`.
#   refunded     -> reembolso total (manual desde el panel de MP)
#   charged_back -> contracargo: el emisor le devolvio la plata al cliente
_REFUND_STATUSES = ("refunded", "charged_back")


def _get_sdk() -> "mercadopago.SDK":
    global _sdk
    if not settings.mp_access_token:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="Pagos no configurados"
        )
    if _sdk is None:
        _sdk = mercadopago.SDK(settings.mp_access_token)
    return _sdk


def _price_for(plan: str) -> int:
    return settings.price_mensual if plan == "mensual" else settings.price_anual


def _valid_signature(
    x_signature: str | None, x_request_id: str | None, data_id: str | None
) -> bool:
    secret = settings.mp_webhook_secret
    if not secret or not x_signature or not data_id:
        return False
    parts = dict(
        p.strip().split("=", 1) for p in x_signature.split(",") if "=" in p
    )
    ts = parts.get("ts")
    v1 = parts.get("v1")
    if not ts or not v1:
        return False
    manifest = f"id:{data_id.lower()};request-id:{x_request_id or ''};ts:{ts};"
    expected = hmac.new(
        secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, v1)


def _parse_external_ref(ref: str | None) -> tuple[uuid.UUID | None, str | None]:
    """Extrae (user_id, plan) del external_reference '{user_id}:{plan}'."""
    if not ref:
        return None, None
    user_id_str, _, plan = ref.partition(":")
    if plan not in PAID_TIERS:
        return None, None
    try:
        return uuid.UUID(user_id_str), plan
    except ValueError:
        return None, None


# ---- Endpoints ----


@router.get("/plans")
def get_plans() -> dict:
    return {
        "mensual": settings.price_mensual,
        "anual": settings.price_anual,
        "currency": settings.currency_id,
    }


@router.get("", response_model=PaymentsPage, dependencies=[Depends(require_admin)])
@limiter.limit(LIST_LIMIT)
def list_payments(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: Literal["approved", "rejected", "cancelled"] | None = Query(
        None, alias="status"
    ),
    db: DbSession = Depends(get_db),
) -> PaymentsPage:
    """Historial de pagos paginado (admin-only). `require_admin` a nivel ENDPOINT
    porque este modulo tiene rutas publicas (/plans, /webhook)."""
    filters = [] if status_filter is None else [Payment.status == status_filter]
    total = db.scalar(select(func.count()).select_from(Payment).where(*filters)) or 0
    rows = db.execute(
        select(Payment, User.email, User.full_name)
        .outerjoin(User, User.id == Payment.user_id)
        .where(*filters)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return PaymentsPage(
        items=[
            PaymentOut(
                id=p.id,
                user_id=p.user_id,
                user_email=email,
                user_full_name=full_name,
                plan=p.plan,
                status=p.status,
                amount=p.amount,
                mp_payment_id=p.mp_payment_id,
                subscription_id=p.subscription_id,
                created_at=p.created_at,
            )
            for p, email, full_name in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
        currency=settings.currency_id,
    )


# ---- Suscripciones (Preapproval API) ----


@router.post("/subscribe", response_model=SubscribeOut)
def create_subscription(
    payload: SubscribeIn, user: User = Depends(require_legal_acceptance),
    db: DbSession = Depends(get_db),
) -> SubscribeOut:
    """Crea una suscripcion recurrente de MP (preapproval standalone con
    status=pending → MP muestra su pagina hosted). Retorna el init_point."""
    plan = payload.plan

    existing = db.scalar(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.mp_status == "authorized",
        )
    )
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Ya tenes una suscripcion activa",
        )

    sdk = _get_sdk()
    price = _price_for(plan)
    freq, freq_type = _FREQUENCY[plan]
    frontend = settings.frontend_url.rstrip("/")
    backend = settings.backend_url.rstrip("/")
    label = plan_copy(plan).label

    preapproval_data = {
        "reason": f"CorpoLab 3D - Plan {label}",
        "auto_recurring": {
            "frequency": freq,
            "frequency_type": freq_type,
            "transaction_amount": float(price),
            "currency_id": settings.currency_id,
        },
        "external_reference": f"{user.id}:{plan}",
        "back_url": f"{frontend}/pago/exito",
        "payer_email": user.email,
        "status": "pending",
        "notification_url": f"{backend}/payments/webhook",
    }

    result = sdk.preapproval().create(preapproval_data)
    if result.get("status") not in (200, 201):
        logger.error("MP preapproval create fallo: %s", result.get("response"))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo crear la suscripcion",
        )

    response = result["response"]
    db.add(
        Subscription(
            user_id=user.id,
            plan=plan,
            mp_preapproval_id=str(response["id"]),
            mp_status="pending",
            mp_payer_id=str(response.get("payer_id") or ""),
        )
    )
    db.commit()

    return SubscribeOut(init_point=response["init_point"])


@router.get("/subscription", response_model=SubscriptionOut | None)
def get_subscription(
    user: User = Depends(require_legal_acceptance),
    db: DbSession = Depends(get_db),
) -> SubscriptionOut | None:
    """Devuelve la suscripcion activa del usuario (la mas reciente no-cancelled),
    o null si no tiene."""
    sub = db.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == user.id,
            Subscription.mp_status != "cancelled",
        )
        .order_by(Subscription.created_at.desc())
    )
    if sub is None:
        return None
    return SubscriptionOut(
        id=sub.id, plan=sub.plan, status=sub.mp_status, created_at=sub.created_at,
    )


@router.post("/cancel-subscription", response_model=CancelSubscriptionOut)
def cancel_subscription(
    user: User = Depends(require_legal_acceptance),
    background: BackgroundTasks = BackgroundTasks(),
    db: DbSession = Depends(get_db),
) -> CancelSubscriptionOut:
    """Cancela la suscripcion activa del usuario en MP y localmente."""
    sub = db.scalar(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.mp_status == "authorized",
        )
    )
    if sub is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="No tenes una suscripcion activa",
        )

    sdk = _get_sdk()
    result = sdk.preapproval().update(
        sub.mp_preapproval_id, {"status": "cancelled"}
    )
    if result.get("status") not in (200, 201):
        logger.error("MP preapproval cancel fallo: %s", result.get("response"))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo cancelar la suscripcion",
        )

    # NO se baja el tier aca a proposito: cancelar corta los cobros FUTUROS, pero
    # el periodo en curso ya esta cobrado y no se reembolsa, asi que el usuario
    # conserva el acceso hasta `expires_at` (igual que el webhook
    # `_handle_subscription_status` para paused/cancelled). Al vencer,
    # `sync_user_tier` -> `downgrade_if_expired` lo pasa a free solo, y
    # `user_is_unlimited` deja de verlo ilimitado (la sub ya no esta `authorized`
    # y el tier quedo vencido). La revocacion INMEDIATA queda reservada al
    # reembolso/contracargo (`_apply_refund`), cuando la plata vuelve.
    sub.mp_status = "cancelled"

    tier = db.scalar(select(UserTier).where(UserTier.user_id == user.id))
    expires_str = format_date(tier.expires_at) if tier else ""

    db.commit()

    label = plan_copy(sub.plan).label
    background.add_task(send_subscription_cancelled_email, user.email, label, expires_str)
    return CancelSubscriptionOut(status="cancelled")


# ---- Checkout Pro (DEPRECATED — legacy one-time) ----


@router.post("/checkout")
def create_checkout(
    payload: SubscribeIn, user: User = Depends(require_legal_acceptance)
) -> dict:
    """DEPRECATED: pago unico. Se mantiene mientras el front viejo siga en prod."""
    sdk = _get_sdk()
    plan = payload.plan
    price = _price_for(plan)
    frontend = settings.frontend_url.rstrip("/")
    backend = settings.backend_url.rstrip("/")
    label = plan_copy(plan).label

    preference = {
        "items": [
            {
                "title": f"CorpoLab 3D - Plan {label}",
                "quantity": 1,
                "unit_price": float(price),
                "currency_id": settings.currency_id,
            }
        ],
        "external_reference": f"{user.id}:{plan}",
        "back_urls": {
            "success": f"{frontend}/pago/exito",
            "failure": f"{frontend}/pago/error",
            "pending": f"{frontend}/pago/pendiente",
        },
        "notification_url": f"{backend}/payments/webhook",
    }
    if frontend.startswith("https://"):
        preference["auto_return"] = "approved"

    result = sdk.preference().create(preference)
    if result.get("status") not in (200, 201):
        logger.error("MP preference create fallo: %s", result.get("response"))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail="No se pudo crear el pago"
        )
    return {"init_point": result["response"]["init_point"]}


# ---- Webhook ----


@router.post("/webhook")
async def webhook(
    request: Request,
    background: BackgroundTasks,
    db: DbSession = Depends(get_db),
) -> dict:
    """Notificacion de Mercado Pago (publico, sin auth). Maneja 3 topics:
    payment (legacy one-time), subscription_preapproval (ciclo de vida),
    subscription_authorized_payment (cobro recurrente)."""
    data_id = request.query_params.get("data.id") or request.query_params.get("id")

    if not _valid_signature(
        request.headers.get("x-signature"),
        request.headers.get("x-request-id"),
        data_id,
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Firma invalida")

    if not data_id:
        return {"status": "ignored"}

    topic = request.query_params.get("type") or request.query_params.get("topic")

    if topic == "subscription_preapproval":
        return _handle_subscription_status(data_id, db, background)
    if topic == "subscription_authorized_payment":
        return _handle_subscription_payment(data_id, db, background)
    if topic == "payment":
        return _handle_one_time_payment(data_id, db, background)

    return {"status": "ignored"}


def _handle_subscription_status(
    data_id: str, db: DbSession, background: BackgroundTasks
) -> dict:
    """Procesa cambio de estado de la suscripcion (authorized/paused/cancelled)."""
    sdk = _get_sdk()
    result = sdk.preapproval().get(data_id)
    if result.get("status") != 200:
        return {"status": "ignored"}
    preapproval = result["response"]
    mp_preapproval_id = str(preapproval["id"])
    mp_status = preapproval.get("status")

    sub = db.scalar(
        select(Subscription).where(
            Subscription.mp_preapproval_id == mp_preapproval_id
        )
    )
    if sub is None:
        return {"status": "ignored"}

    if sub.mp_status == mp_status:
        return {"status": "ok"}

    sub.mp_status = mp_status
    sub.mp_payer_id = str(preapproval.get("payer_id") or sub.mp_payer_id or "")

    user = db.get(User, sub.user_id)
    if user is None:
        db.commit()
        return {"status": "ok"}

    to_email = user.email
    label, period_every, period_each = plan_copy(sub.plan)
    now = datetime.now(timezone.utc)

    if mp_status == "authorized":
        tier = activate_subscription_tier(db, user.id, sub.plan, sub.id, now)
        next_charge_at = format_date(tier.expires_at)
        # `or {}` y no un default en el .get(): MP puede mandar la clave con null,
        # y ahi el default no aplica y el .get() encadenado revienta.
        amount = (
            (preapproval.get("auto_recurring") or {}).get("transaction_amount")
            or _price_for(sub.plan)
        )
        db.commit()
        background.add_task(
            send_subscription_activated_email,
            to_email,
            label,
            format_amount(amount),
            settings.currency_id,
            period_every,
            period_each,
            next_charge_at,
        )
    elif mp_status in ("paused", "cancelled"):
        # NO se baja el tier aca a proposito: cancelar/pausar corta los cobros
        # FUTUROS, pero el periodo en curso ya esta cobrado y no se reembolsa, asi
        # que el usuario conserva el acceso hasta `expires_at` (como cualquier SaaS).
        # Al vencer, `sync_user_tier` -> `downgrade_if_expired` lo pasa a free solo,
        # y `user_is_unlimited` deja de verlo ilimitado porque la suscripcion ya no
        # esta `authorized` y el tier quedo vencido.
        # La revocacion INMEDIATA queda reservada al reembolso/contracargo
        # (`_apply_refund`), que es cuando la plata efectivamente vuelve.
        tier = db.scalar(select(UserTier).where(UserTier.user_id == user.id))
        expires_str = format_date(tier.expires_at) if tier else ""
        db.commit()
        if mp_status == "paused":
            background.add_task(send_subscription_paused_email, to_email, label)
        else:
            background.add_task(
                send_subscription_cancelled_email, to_email, label, expires_str
            )
    else:
        db.commit()

    return {"status": "ok"}


def _apply_refund(
    payment_data: dict, db: DbSession, background: BackgroundTasks
) -> dict | None:
    """Si la plata volvio (reembolso manual desde el panel de MP, o contracargo),
    revierte el tier del usuario a free EN EL ACTO y marca el `Payment`.

    Devuelve la respuesta del webhook, o `None` si el pago no es un reembolso (y
    entonces el caller sigue con su flujo normal).

    Se llama ANTES del guard de idempotencia por `mp_payment_id` de los handlers:
    MP re-notifica el reembolso con el MISMO id de pago, asi que ese guard veria la
    fila ya existente y cortaria con {"status": "ok"} sin hacer nada. La
    idempotencia de esta funcion es distinta: mira el ESTADO de la fila, no su
    existencia, para poder procesar la transicion approved -> refunded una sola vez.

    Ojo: solo cubre reembolsos TOTALES. Un reembolso parcial deja el pago en
    `approved` con `status_detail=partially_refunded`, no entra aca, y el usuario
    conserva el tier — que es lo correcto: le devolviste una parte, no todo.
    """
    mp_status = payment_data.get("status")
    if mp_status not in _REFUND_STATUSES:
        return None

    mp_payment_id = str(payment_data["id"])
    payment = db.scalar(select(Payment).where(Payment.mp_payment_id == mp_payment_id))

    if payment is not None and payment.status in _REFUND_STATUSES:
        return {"status": "ok"}  # ya aplicado

    # El `Payment` es el vinculo confiable con el usuario; el external_reference
    # es el fallback para un pago que nunca llego a registrarse.
    user_id = payment.user_id if payment is not None else None
    plan = payment.plan if payment is not None else None
    if user_id is None:
        user_id, plan = _parse_external_ref(payment_data.get("external_reference"))
    if user_id is None:
        return {"status": "ignored"}

    if payment is not None:
        payment.status = mp_status

    user = db.get(User, user_id)
    if user is None:
        db.commit()
        return {"status": "ok"}

    # Baja a free y desengancha la suscripcion del tier, asi `user_is_unlimited`
    # devuelve False aunque la suscripcion siga `authorized` en MP (caso: se
    # reembolso sin cancelar).
    deactivate_subscription_tier(db, user.id)

    # Reembolsar = el cliente se va: cancelar tambien la suscripcion para cortar
    # los cobros FUTUROS (reembolsar en MP no cancela la preapproval por si solo).
    # Se busca por `payment.subscription_id` (se setea al registrar el cobro
    # recurrente) y, como fallback, por la sub non-cancelled mas reciente del
    # usuario. Best-effort: si el update a MP falla, se loguea pero NO se rompe el
    # webhook (la revocacion del tier ya esta hecha y debe persistir igual). Un
    # reembolso de pago legacy one-time no tiene sub asociada y salta este bloque.
    sub = None
    if payment is not None and payment.subscription_id is not None:
        sub = db.get(Subscription, payment.subscription_id)
    if sub is None:
        sub = db.scalar(
            select(Subscription)
            .where(
                Subscription.user_id == user.id,
                Subscription.mp_status != "cancelled",
            )
            .order_by(Subscription.created_at.desc())
        )
    if sub is not None and sub.mp_status != "cancelled":
        try:
            cancel_result = _get_sdk().preapproval().update(
                sub.mp_preapproval_id, {"status": "cancelled"}
            )
            if cancel_result.get("status") in (200, 201):
                sub.mp_status = "cancelled"
            else:
                logger.error(
                    "MP preapproval cancel en reembolso fallo (sub %s): %s",
                    sub.id, cancel_result.get("response"),
                )
        except Exception:  # noqa: BLE001 - best-effort, no romper el webhook
            logger.exception(
                "Error cancelando preapproval en reembolso (sub %s)", sub.id
            )

    db.commit()
    logger.info(
        "Pago %s en estado %s: tier del usuario %s revertido a free",
        mp_payment_id, mp_status, user.id,
    )

    # Aviso al usuario. Va DESPUES del commit: si el mail falla, la revocacion ya
    # esta persistida (el `_send` de app/email.py no levanta nunca).
    amount = payment.amount if payment is not None else payment_data.get("transaction_amount")
    label = "Mensual" if plan == "mensual" else "Anual" if plan == "anual" else "pago"
    background.add_task(
        send_refund_email,
        user.email,
        label,
        format_amount(int(amount or 0)),
        settings.currency_id,
        mp_status == "charged_back",
    )
    return {"status": "ok"}


def _handle_subscription_payment(
    data_id: str, db: DbSession, background: BackgroundTasks
) -> dict:
    """Procesa un cobro recurrente de una suscripcion."""
    sdk = _get_sdk()
    result = sdk.payment().get(data_id)
    if result.get("status") != 200:
        return {"status": "ignored"}
    payment_data = result["response"]
    mp_status = payment_data.get("status")

    refunded = _apply_refund(payment_data, db, background)
    if refunded is not None:
        return refunded

    if mp_status not in ("approved", "rejected", "cancelled"):
        return {"status": "ignored"}

    mp_payment_id = str(payment_data["id"])
    if db.scalar(select(Payment).where(Payment.mp_payment_id == mp_payment_id)):
        return {"status": "ok"}

    user_id, plan = _parse_external_ref(payment_data.get("external_reference"))
    if user_id is None or plan is None:
        return {"status": "ignored"}
    user = db.get(User, user_id)
    if user is None:
        return {"status": "ignored"}

    sub = db.scalar(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.mp_status.in_(("authorized", "pending")),
            Subscription.plan == plan,
        ).order_by(Subscription.created_at.desc())
    )

    amount = int(payment_data.get("transaction_amount") or 0)
    to_email = user.email
    label, period_every, period_each = plan_copy(plan)

    db.add(
        Payment(
            user_id=user_id,
            plan=plan,
            mp_payment_id=mp_payment_id,
            status=mp_status,
            amount=amount,
            subscription_id=sub.id if sub else None,
        )
    )

    expires_str = ""
    if mp_status == "approved" and sub:
        now = datetime.now(timezone.utc)
        tier = db.scalar(
            select(UserTier).where(UserTier.user_id == user_id).with_for_update()
        )
        if tier and tier.subscription_id == sub.id:
            tier.expires_at = expiry_for(plan, now)
            tier.expiry_warning_sent_at = None
            expires_str = format_date(tier.expires_at)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "ok"}

    if mp_status == "approved":
        background.add_task(
            send_payment_approved_email,
            to_email,
            label,
            format_amount(amount),
            settings.currency_id,
            expires_str,
            is_recurring=True,
            period_every=period_every,
            period_each=period_each,
        )
    else:
        background.add_task(
            send_subscription_charge_failed_email, to_email, label,
        )

    return {"status": "ok"}


def _handle_one_time_payment(
    data_id: str, db: DbSession, background: BackgroundTasks
) -> dict:
    """Procesa un pago unico de Checkout Pro (legacy)."""
    sdk = _get_sdk()
    result = sdk.payment().get(data_id)
    if result.get("status") != 200:
        return {"status": "ignored"}
    payment_data = result["response"]
    mp_status = payment_data.get("status")

    refunded = _apply_refund(payment_data, db, background)
    if refunded is not None:
        return refunded

    if mp_status not in ("approved", "rejected", "cancelled"):
        return {"status": "ignored"}

    mp_payment_id = str(payment_data["id"])
    if db.scalar(select(Payment).where(Payment.mp_payment_id == mp_payment_id)):
        return {"status": "ok"}

    user_id, plan = _parse_external_ref(payment_data.get("external_reference"))
    if user_id is None or plan is None:
        return {"status": "ignored"}
    user = db.get(User, user_id)
    if user is None:
        return {"status": "ignored"}

    to_email = user.email
    label = plan_copy(plan).label
    amount = int(payment_data.get("transaction_amount") or 0)

    if mp_status == "approved":
        now = datetime.now(timezone.utc)
        tier = activate_paid_tier(db, user_id, plan, now)
        expires_str = format_date(tier.expires_at)
        db.add(
            Payment(
                user_id=user_id,
                plan=plan,
                mp_payment_id=mp_payment_id,
                status="approved",
                amount=amount,
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return {"status": "ok"}
        background.add_task(
            send_payment_approved_email,
            to_email,
            label,
            format_amount(amount),
            settings.currency_id,
            expires_str,
        )
        return {"status": "ok"}

    db.add(
        Payment(
            user_id=user_id,
            plan=plan,
            mp_payment_id=mp_payment_id,
            status=mp_status,
            amount=amount,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "ok"}
    retry_link = f"{settings.frontend_url.rstrip('/')}/pricing"
    background.add_task(send_payment_rejected_email, to_email, label, retry_link)
    return {"status": "ok"}


# --- Boton de arrepentimiento (Ley 24.240 art. 34) ---


@router.post("/withdrawal", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("3/minute")
def request_withdrawal(
    request: Request,
    payload: WithdrawalRequestIn,
    background: BackgroundTasks,
) -> None:
    background.add_task(
        send_withdrawal_request_email,
        payload.full_name,
        payload.email,
        payload.reason,
    )

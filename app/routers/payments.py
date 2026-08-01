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
from app.deps import get_current_user, require_admin
from app.email import (
    send_payment_approved_email,
    send_payment_rejected_email,
    send_subscription_activated_email,
    send_subscription_cancelled_email,
    send_subscription_charge_failed_email,
    send_subscription_paused_email,
)
from app.models import Payment, Subscription, User
from app.ratelimit import limiter
from app.routers.tiers import (
    PAID_TIERS,
    activate_paid_tier,
    activate_subscription_tier,
    deactivate_subscription_tier,
    expiry_for,
)
from app.schemas import (
    CancelSubscriptionOut,
    PaymentOut,
    PaymentsPage,
    SubscribeIn,
    SubscribeOut,
    SubscriptionOut,
)

router = APIRouter(prefix="/payments", tags=["payments"])

logger = logging.getLogger("payments")

LIST_LIMIT = "60/minute"

_sdk: "mercadopago.SDK | None" = None

_FREQUENCY = {"mensual": (1, "months"), "anual": (12, "months")}


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
        .join(User, User.id == Payment.user_id)
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
    payload: SubscribeIn, user: User = Depends(get_current_user),
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
    label = "Mensual" if plan == "mensual" else "Anual"

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
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
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

    sub.mp_status = "cancelled"
    deactivate_subscription_tier(db, user.id)
    db.commit()

    label = "Mensual" if sub.plan == "mensual" else "Anual"
    background.add_task(send_subscription_cancelled_email, user.email, label)
    return CancelSubscriptionOut(status="cancelled")


# ---- Checkout Pro (DEPRECATED — legacy one-time) ----


@router.post("/checkout")
def create_checkout(
    payload: SubscribeIn, user: User = Depends(get_current_user)
) -> dict:
    """DEPRECATED: pago unico. Se mantiene mientras el front viejo siga en prod."""
    sdk = _get_sdk()
    plan = payload.plan
    price = _price_for(plan)
    frontend = settings.frontend_url.rstrip("/")
    backend = settings.backend_url.rstrip("/")
    label = "Mensual" if plan == "mensual" else "Anual"

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
    label = "Mensual" if sub.plan == "mensual" else "Anual"
    now = datetime.now(timezone.utc)

    if mp_status == "authorized":
        activate_subscription_tier(db, user.id, sub.plan, sub.id, now)
        db.commit()
        background.add_task(send_subscription_activated_email, to_email, label)
    elif mp_status in ("paused", "cancelled"):
        deactivate_subscription_tier(db, user.id)
        db.commit()
        if mp_status == "paused":
            background.add_task(send_subscription_paused_email, to_email, label)
        else:
            background.add_task(send_subscription_cancelled_email, to_email, label)
    else:
        db.commit()

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
    label = "Mensual" if plan == "mensual" else "Anual"

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

    if mp_status == "approved" and sub:
        now = datetime.now(timezone.utc)
        from app.models import UserTier
        tier = db.scalar(
            select(UserTier).where(UserTier.user_id == user_id).with_for_update()
        )
        if tier and tier.subscription_id == sub.id:
            tier.expires_at = expiry_for(plan, now)
            tier.expiry_warning_sent_at = None

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "ok"}

    if mp_status == "approved":
        expires_str = "-"
        background.add_task(
            send_payment_approved_email,
            to_email,
            label,
            f"{amount:,.0f}".replace(",", "."),
            settings.currency_id,
            expires_str,
        )
    else:
        retry_link = f"{settings.frontend_url.rstrip('/')}/pricing"
        background.add_task(
            send_subscription_charge_failed_email, to_email, label, retry_link,
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
    label = "Mensual" if plan == "mensual" else "Anual"
    amount = int(payment_data.get("transaction_amount") or 0)

    if mp_status == "approved":
        now = datetime.now(timezone.utc)
        tier = activate_paid_tier(db, user_id, plan, now)
        expires_str = tier.expires_at.strftime("%d/%m/%Y") if tier.expires_at else "-"
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
            f"{amount:,.0f}".replace(",", "."),
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

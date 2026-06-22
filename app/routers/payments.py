import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

import mercadopago
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import Payment, User
from app.routers.tiers import PAID_TIERS, activate_paid_tier

# Pagos con Mercado Pago (Checkout Pro / pago unico). Reglas de oro:
#  - El tier se activa SOLO desde el webhook con firma validada; el redirect del
#    navegador (back_urls) NO se confia (es spoofeable).
#  - Precios SIEMPRE del backend: el cliente manda solo `{ plan }`; el monto sale
#    de `config`. La UI tambien lee los precios de aca (GET /plans). Anti-tamper.
#  - Idempotencia por Payment.mp_payment_id UNIQUE: MP reintenta el webhook.
router = APIRouter(prefix="/payments", tags=["payments"])

logger = logging.getLogger("payments")

# SDK lazy: se crea una sola vez al primer uso (requiere el access token; sin el,
# los endpoints que lo necesitan responden 503, pero la app arranca igual).
_sdk: "mercadopago.SDK | None" = None


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
    """Valida la firma del webhook (HMAC-SHA256). MP manda el header `x-signature`
    como `ts=<ts>,v1=<hash>`; el manifest firmado es
    `id:<data.id>;request-id:<x-request-id>;ts:<ts>;` (data.id en minusculas).
    Sin secret configurado o firma que no matchea -> False (se rechaza con 401)."""
    secret = settings.mp_webhook_secret
    # TEMP DEBUG: quitar despues de diagnosticar la firma del webhook.
    logger.warning(
        "WEBHOOK DEBUG sig=%r reqid=%r data_id=%r secret_len=%s",
        x_signature, x_request_id, data_id, len(secret) if secret else 0,
    )
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
    # TEMP DEBUG
    logger.warning(
        "WEBHOOK DEBUG manifest=%r expected=%s v1=%s match=%s",
        manifest, expected, v1, hmac.compare_digest(expected, v1),
    )
    return hmac.compare_digest(expected, v1)


# --- Schemas ---


class CheckoutIn(BaseModel):
    # SOLO el plan, jamas el monto: el servidor resuelve el precio desde config.
    plan: Literal["mensual", "anual"]


class CheckoutOut(BaseModel):
    init_point: str


class PlansOut(BaseModel):
    mensual: int
    anual: int
    currency: str


# --- Endpoints ---


@router.get("/plans", response_model=PlansOut)
def get_plans() -> PlansOut:
    """Publico: precios desde config (unica fuente de verdad). La UI los lee para
    MOSTRAR los montos -> nada hardcodeado en el front que pueda diferir del cobro."""
    return PlansOut(
        mensual=settings.price_mensual,
        anual=settings.price_anual,
        currency=settings.currency_id,
    )


@router.post("/checkout", response_model=CheckoutOut)
def create_checkout(
    payload: CheckoutIn, user: User = Depends(get_current_user)
) -> CheckoutOut:
    """Crea la preference de Checkout Pro y devuelve el init_point (URL a la que el
    front redirige). El monto lo fija el servidor segun el plan (anti-tamper)."""
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
        # user_id:plan -> el webhook sabe a quien y que activar (no se confia en el
        # cliente: igual se re-valida el monto contra el pago real de MP).
        "external_reference": f"{user.id}:{plan}",
        "back_urls": {
            "success": f"{frontend}/pago/exito",
            "failure": f"{frontend}/pago/error",
            "pending": f"{frontend}/pago/pendiente",
        },
        "notification_url": f"{backend}/payments/webhook",
    }
    # auto_return (volver solo al sitio tras aprobar) exige que back_urls.success
    # sea una URL PUBLICA; MP rechaza http://localhost. Solo lo activamos cuando el
    # frontend es https (prod). En dev el redirect a localhost igual funciona en el
    # navegador del comprador, solo que hay que tocar "Volver al sitio".
    if frontend.startswith("https://"):
        preference["auto_return"] = "approved"

    result = sdk.preference().create(preference)
    if result.get("status") not in (200, 201):
        # MP devuelve el motivo en response.message/error; lo logueamos para no
        # quedar a ciegas (no lo exponemos al cliente).
        logger.error("MP preference create fallo: %s", result.get("response"))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail="No se pudo crear el pago"
        )
    return CheckoutOut(init_point=result["response"]["init_point"])


@router.post("/webhook")
async def webhook(request: Request, db: DbSession = Depends(get_db)) -> dict:
    """Notificacion de Mercado Pago (publico, sin auth). Valida la firma SIEMPRE;
    re-consulta el pago real a MP y, si esta aprobado, activa el tier + registra el
    Payment en una sola transaccion. Idempotente (mp_payment_id UNIQUE). Responde
    siempre 200 salvo firma invalida (401), para que MP no reintente en vano."""
    data_id = request.query_params.get("data.id") or request.query_params.get("id")

    if not _valid_signature(
        request.headers.get("x-signature"),
        request.headers.get("x-request-id"),
        data_id,
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Firma invalida")

    # Solo procesamos notificaciones de pago (MP manda otras: merchant_order, etc.).
    topic = request.query_params.get("type") or request.query_params.get("topic")
    if (topic and topic != "payment") or not data_id:
        return {"status": "ignored"}

    sdk = _get_sdk()
    result = sdk.payment().get(data_id)
    if result.get("status") != 200:
        return {"status": "ignored"}
    payment = result["response"]
    if payment.get("status") != "approved":
        return {"status": "ignored"}

    mp_payment_id = str(payment["id"])
    # Idempotencia: si ya lo procesamos, cortamos sin re-activar ni duplicar.
    if db.scalar(select(Payment).where(Payment.mp_payment_id == mp_payment_id)):
        return {"status": "ok"}

    # user_id:plan viene del external_reference que pusimos al crear la preference.
    user_id_str, _, plan = (payment.get("external_reference") or "").partition(":")
    if plan not in PAID_TIERS:
        return {"status": "ignored"}
    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        return {"status": "ignored"}
    if db.get(User, user_id) is None:
        return {"status": "ignored"}

    now = datetime.now(timezone.utc)
    activate_paid_tier(db, user_id, plan, now)
    db.add(
        Payment(
            user_id=user_id,
            plan=plan,
            mp_payment_id=mp_payment_id,
            status="approved",
            amount=int(payment.get("transaction_amount") or 0),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Carrera: otra entrega del mismo webhook ya inserto el Payment (UNIQUE).
        db.rollback()
    return {"status": "ok"}

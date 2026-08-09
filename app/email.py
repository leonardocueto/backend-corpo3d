"""Envio de emails transaccionales (Resend).

Capa aislada: el resto del codigo solo llama a los `send_*`. Si manana se cambia de
proveedor (Brevo, SES...), se toca SOLO este archivo. El HTML lo arma Jinja2 en
`app/mailing/` (templates branded con header/footer + tema claro); aca solo va el envio.

Sin `RESEND_API_KEY` (tipico en dev) no se envia nada: se LOGUEA el link/codigo para
poder probar el flujo local sin proveedor. Pensado para correr en BackgroundTask, asi
que NUNCA levanta: cualquier fallo se loguea y se traga (no rompe la request ni filtra
por timing si el email existe o no).
"""

import logging

import httpx

from app.config import settings
from app.mailing.render import render_email

logger = logging.getLogger("app.email")

RESEND_ENDPOINT = "https://api.resend.com/emails"


def _send(to_email: str, subject: str, html: str) -> None:
    """POST a Resend. Nunca levanta (corre en BackgroundTask): loguea y traga el error."""
    try:
        resp = httpx.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.email_from,
                "to": [to_email],
                "subject": subject,
                "html": html,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        # No re-lanzamos: el endpoint responde igual (anti-enumeracion / anti-timing).
        logger.error("Fallo enviando '%s' a %s: %s", subject, to_email, exc)


def send_password_reset_email(to_email: str, reset_link: str) -> None:
    if not settings.resend_api_key:
        # Dev / sin proveedor: dejamos el link en los logs para probar el flujo.
        logger.warning("[DEV] Reset password link para %s: %s", to_email, reset_link)
        return
    html = render_email(
        "reset_password.html", reset_link=reset_link, minutes=settings.reset_token_minutes
    )
    _send(to_email, "Restablecer tu contraseña - CorpoLab 3D", html)


def send_signup_verification_email(to_email: str, verify_link: str) -> None:
    """Manda el link de confirmacion de alta (double opt-in). La cuenta se crea recien
    cuando el usuario abre este link."""
    if not settings.resend_api_key:
        # Dev / sin proveedor: dejamos el link en los logs para probar el flujo.
        logger.warning("[DEV] Signup verification link para %s: %s", to_email, verify_link)
        return
    html = render_email(
        "signup_verification.html", verify_link=verify_link, minutes=settings.signup_token_minutes
    )
    _send(to_email, "Confirma tu cuenta - CorpoLab 3D", html)


def send_login_otp_email(to_email: str, code: str) -> None:
    """Manda el codigo OTP del 2do factor de login."""
    if not settings.resend_api_key:
        # Dev / sin proveedor: dejamos el codigo en los logs para probar el flujo.
        logger.warning("[DEV] OTP para %s: %s", to_email, code)
        return
    html = render_email(
        "login_otp.html", code=code, minutes=settings.otp_minutes, email=to_email
    )
    _send(to_email, "Tu codigo de acceso - CorpoLab 3D", html)


def send_welcome_email(to_email: str, full_name: str | None) -> None:
    """Bienvenida tras crear la cuenta (alta self-serve confirmada)."""
    if not settings.resend_api_key:
        # Dev / sin proveedor: dejamos constancia en los logs.
        logger.warning("[DEV] Welcome email para %s", to_email)
        return
    # verify-signup NO inicia sesion: el CTA lleva al login.
    html = render_email(
        "welcome.html",
        full_name=full_name,
        app_link=f"{settings.frontend_url.rstrip('/')}/ingresar",
    )
    _send(to_email, "¡Bienvenido a CorpoLab 3D!", html)


def send_payment_approved_email(
    to_email: str, plan_label: str, amount: str, currency: str, expires_at: str
) -> None:
    """Confirma un pago aprobado (tier activado). El caller pasa el monto/vencimiento
    ya formateados como texto (esta capa solo arma y envia)."""
    if not settings.resend_api_key:
        logger.warning("[DEV] Payment approved email para %s (%s)", to_email, plan_label)
        return
    html = render_email(
        "payment_approved.html",
        plan_label=plan_label,
        amount=amount,
        currency=currency,
        expires_at=expires_at,
        app_link=f"{settings.frontend_url.rstrip('/')}/editor",
    )
    _send(to_email, "Pago confirmado - CorpoLab 3D", html)


def send_payment_rejected_email(
    to_email: str, plan_label: str, retry_link: str
) -> None:
    """Avisa que un pago fue rechazado (no se realizo cargo). Incluye link para reintentar."""
    if not settings.resend_api_key:
        logger.warning("[DEV] Payment rejected email para %s (%s)", to_email, plan_label)
        return
    html = render_email(
        "payment_rejected.html", plan_label=plan_label, retry_link=retry_link
    )
    _send(to_email, "No pudimos procesar tu pago - CorpoLab 3D", html)


def send_tier_expiring_email(
    to_email: str, plan_label: str, expires_at: str, renew_link: str
) -> None:
    """Aviso de vencimiento proximo del tier pago (job diario, ~10 dias antes). El
    caller pasa la fecha ya formateada; esta capa solo arma y envia."""
    if not settings.resend_api_key:
        logger.warning("[DEV] Tier expiring email para %s (%s, vence %s)", to_email, plan_label, expires_at)
        return
    html = render_email(
        "tier_expiring.html",
        plan_label=plan_label,
        expires_at=expires_at,
        renew_link=renew_link,
    )
    _send(to_email, "Tu plan está por vencer - CorpoLab 3D", html)


# --- Suscripciones (MP Preapproval) ---


def send_subscription_activated_email(
    to_email: str, plan_label: str, amount: str, currency: str
) -> None:
    if not settings.resend_api_key:
        logger.warning("[DEV] Subscription activated email para %s (%s)", to_email, plan_label)
        return
    html = render_email(
        "subscription_activated.html",
        plan_label=plan_label,
        amount=amount,
        currency=currency,
        app_link=f"{settings.frontend_url.rstrip('/')}/editor",
    )
    _send(to_email, "Suscripción activada - CorpoLab 3D", html)


def send_refund_email(
    to_email: str, plan_label: str, amount: str, currency: str, is_chargeback: bool
) -> None:
    """Avisa que la plata volvio (reembolso manual o contracargo) y que la cuenta
    quedo en free. El caller pasa el monto ya formateado como texto."""
    if not settings.resend_api_key:
        logger.warning(
            "[DEV] Refund email para %s (%s, chargeback=%s)", to_email, plan_label, is_chargeback
        )
        return
    html = render_email(
        "refund_processed.html",
        plan_label=plan_label,
        amount=amount,
        currency=currency,
        is_chargeback=is_chargeback,
        reactivate_link=f"{settings.frontend_url.rstrip('/')}/pricing",
    )
    subject = (
        "Contracargo registrado - CorpoLab 3D" if is_chargeback
        else "Devolución procesada - CorpoLab 3D"
    )
    _send(to_email, subject, html)


def send_subscription_cancelled_email(to_email: str, plan_label: str) -> None:
    if not settings.resend_api_key:
        logger.warning("[DEV] Subscription cancelled email para %s (%s)", to_email, plan_label)
        return
    html = render_email(
        "subscription_cancelled.html",
        plan_label=plan_label,
        reactivate_link=f"{settings.frontend_url.rstrip('/')}/pricing",
    )
    _send(to_email, "Suscripción cancelada - CorpoLab 3D", html)


def send_subscription_paused_email(to_email: str, plan_label: str) -> None:
    if not settings.resend_api_key:
        logger.warning("[DEV] Subscription paused email para %s (%s)", to_email, plan_label)
        return
    html = render_email(
        "subscription_paused.html",
        plan_label=plan_label,
    )
    _send(to_email, "Suscripción pausada - CorpoLab 3D", html)


def send_subscription_charge_failed_email(to_email: str, plan_label: str) -> None:
    if not settings.resend_api_key:
        logger.warning("[DEV] Subscription charge failed email para %s (%s)", to_email, plan_label)
        return
    html = render_email(
        "subscription_charge_failed.html",
        plan_label=plan_label,
    )
    _send(to_email, "No pudimos cobrar tu suscripción - CorpoLab 3D", html)


def send_withdrawal_request_email(
    customer_name: str, customer_email: str, reason: str | None
) -> None:
    """Notifica al admin sobre una solicitud de arrepentimiento (Ley 24.240 art. 34).
    Se envia a info@corpolab3d.com (no al cliente). El motivo es opcional: el
    consumidor puede revocar sin justificar."""
    reason_text = (reason or "").strip() or "(no indico motivo)"
    if not settings.resend_api_key:
        logger.warning(
            "[DEV] Withdrawal request de %s (%s): %s",
            customer_name, customer_email, reason_text,
        )
        return
    html = render_email(
        "withdrawal_request.html",
        customer_name=customer_name,
        customer_email=customer_email,
        reason=reason_text,
    )
    _send("info@corpolab3d.com", f"Solicitud de arrepentimiento - {customer_name}", html)

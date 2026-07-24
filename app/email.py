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

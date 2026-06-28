"""Envio de emails transaccionales (Resend).

Capa aislada: el resto del codigo solo llama a `send_password_reset_email`. Si
manana se cambia de proveedor (Brevo, SES...), se toca SOLO este archivo.

Sin `RESEND_API_KEY` (tipico en dev) no se envia nada: se LOGUEA el link para
poder probar el flujo local sin proveedor. Pensado para correr en BackgroundTask,
asi que NUNCA levanta: cualquier fallo se loguea y se traga (no rompe la request
ni filtra por timing si el email existe o no).
"""

import logging
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger("app.email")

RESEND_ENDPOINT = "https://api.resend.com/emails"

# Template del email de OTP (HTML email-safe, tema de marca). Se lee una sola vez
# al importar el modulo; los placeholders {{code}}/{{minutes}}/{{email}} se sustituyen
# con str.replace (no .format, por las llaves del HTML).
_OTP_TEMPLATE_PATH = Path(__file__).parent / "templates" / "otp.html"
try:
    _OTP_TEMPLATE = _OTP_TEMPLATE_PATH.read_text(encoding="utf-8")
except OSError:
    # Fallback minimo si falta el archivo: el flujo no se cae por el template.
    _OTP_TEMPLATE = (
        "<p>Tu codigo de acceso a CorpoLab 3D es <strong>{{code}}</strong>.</p>"
        "<p>Vence en {{minutes}} minutos. Si no fuiste vos, ignora este mensaje.</p>"
    )


def send_password_reset_email(to_email: str, reset_link: str) -> None:
    if not settings.resend_api_key:
        # Dev / sin proveedor: dejamos el link en los logs para probar el flujo.
        logger.warning("[DEV] Reset password link para %s: %s", to_email, reset_link)
        return

    html = (
        f'<p>Recibimos un pedido para restablecer tu contraseña en CorpoLab 3D.</p>'
        f'<p><a href="{reset_link}">Crear una nueva contraseña</a></p>'
        f'<p>El enlace vence en {settings.reset_token_minutes} minutos. '
        f'Si no fuiste vos, ignora este mensaje.</p>'
    )
    try:
        resp = httpx.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.email_from,
                "to": [to_email],
                "subject": "Restablecer tu contraseña - CorpoLab 3D",
                "html": html,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        # No re-lanzamos: el endpoint responde igual (anti-enumeracion).
        logger.error("Fallo enviando reset email a %s: %s", to_email, exc)


def send_login_otp_email(to_email: str, code: str) -> None:
    """Manda el codigo OTP del 2do factor de login. Mismo patron que el reset:
    sin `RESEND_API_KEY` (dev) LOGUEA el codigo y vuelve; nunca levanta (corre en
    BackgroundTask)."""
    if not settings.resend_api_key:
        # Dev / sin proveedor: dejamos el codigo en los logs para probar el flujo.
        logger.warning("[DEV] OTP para %s: %s", to_email, code)
        return

    html = (
        _OTP_TEMPLATE.replace("{{code}}", code)
        .replace("{{minutes}}", str(settings.otp_minutes))
        .replace("{{email}}", to_email)
    )
    try:
        resp = httpx.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.email_from,
                "to": [to_email],
                "subject": "Tu codigo de acceso - CorpoLab 3D",
                "html": html,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        # No re-lanzamos: el login responde igual (anti-enumeracion / anti-timing).
        logger.error("Fallo enviando OTP a %s: %s", to_email, exc)

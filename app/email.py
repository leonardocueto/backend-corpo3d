"""Envio de emails transaccionales (Resend).

Capa aislada: el resto del codigo solo llama a `send_password_reset_email`. Si
manana se cambia de proveedor (Brevo, SES...), se toca SOLO este archivo.

Sin `RESEND_API_KEY` (tipico en dev) no se envia nada: se LOGUEA el link para
poder probar el flujo local sin proveedor. Pensado para correr en BackgroundTask,
asi que NUNCA levanta: cualquier fallo se loguea y se traga (no rompe la request
ni filtra por timing si el email existe o no).
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger("app.email")

RESEND_ENDPOINT = "https://api.resend.com/emails"


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

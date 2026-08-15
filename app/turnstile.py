"""Verificacion de tokens de Cloudflare Turnstile (captcha invisible).

Capa aislada del proveedor, mismo criterio que `app/email.py`: el resto del codigo
solo llama a `verify()`. Si manana se cambia de proveedor se toca SOLO este archivo.

Por que Turnstile y no reCAPTCHA (decision 2026-07-24, ejecutada 2026-08-15): ya
estamos en Cloudflare, es gratis sin tope util (1M siteverify/mes en el plan Free) y
no necesita cuenta de Google Cloud. reCAPTCHA recorto su free tier a 10.000
assessments/mes el 2026-04-02, y pasado ese numero **sin billing devuelve 429 y deja
de proteger**. Ver `CLAUDE.md` -> "Seguridad".

El token lo genera el front (widget invisible) y viaja en el header
`CF-Turnstile-Response`. Validarlo ACA es lo que hace que sirva: un captcha que solo
corre en el navegador se saltea con un `curl`, y el guard de origen NO cubre estos
endpoints (son publicos por definicion).
"""

import logging
from dataclasses import dataclass, field

import httpx

from app.config import settings

logger = logging.getLogger("app.turnstile")

SITEVERIFY_ENDPOINT = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Timeout corto a proposito: esto corre en el camino critico de un login, no en un
# BackgroundTask como los mails (que usan 10s). Mejor fallar abierto rapido que
# tener al usuario esperando.
_TIMEOUT_SECONDS = 5


@dataclass
class VerifyResult:
    """Resultado de un siteverify. `ok=True` incluye el caso fail-open (Cloudflare
    inalcanzable), que se distingue en los logs por `error_codes=["unreachable"]`."""

    ok: bool
    error_codes: list[str] = field(default_factory=list)


def verify(token: str | None, ip: str, expected_action: str) -> VerifyResult:
    """Valida un token contra el siteverify de Cloudflare.

    - Sin token (el front no lo mando, o la site key no esta cargada en el build)
      -> `ok=False` sin salir a la red. Es el caso que hay que mirar en los logs
      durante la fase de observacion: si aparece con trafico legitimo, falta la
      env `NUXT_PUBLIC_TURNSTILE_SITE_KEY` en ese entorno.
    - FAIL-OPEN si Cloudflare no responde (timeout / red): devuelve `ok=True`
      **incluso con enforce activo**. Que Cloudflare tenga un mal dia no puede
      dejar a nadie sin poder registrarse ni recuperar su contraseña. Un token
      invalido, en cambio, si es `ok=False`.
    - Se compara la `action` que devuelve Cloudflare contra la esperada: sin eso,
      un token obtenido en `/recuperar` sirve para pegarle a `/signup`.
    """
    if not token:
        return VerifyResult(ok=False, error_codes=["missing-input-response"])

    try:
        resp = httpx.post(
            SITEVERIFY_ENDPOINT,
            data={
                "secret": settings.turnstile_secret_key,
                "response": token,
                "remoteip": ip,
            },
            timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        # ValueError cubre un body que no es JSON (respuesta de un proxy, etc.).
        logger.warning("Turnstile inalcanzable (fail-open) para action=%s: %s", expected_action, exc)
        return VerifyResult(ok=True, error_codes=["unreachable"])

    if not body.get("success"):
        # Codigos utiles: `timeout-or-duplicate` = token ya gastado o vencido (los
        # tokens son de un solo uso y viven 300s; si aparece seguido, el front no
        # esta llamando a turnstile.reset() entre submits);
        # `invalid-input-response` = token malformado o de otra site key.
        return VerifyResult(ok=False, error_codes=list(body.get("error-codes") or []))

    # `action` solo viene si el widget la mando. Si viene y no coincide, el token
    # es de otro formulario: no vale.
    got_action = body.get("action")
    if got_action and got_action != expected_action:
        return VerifyResult(ok=False, error_codes=[f"action-mismatch:{got_action}"])

    return VerifyResult(ok=True)

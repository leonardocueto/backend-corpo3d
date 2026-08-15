import logging
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, joinedload

from app import turnstile
from app.config import settings
from app.database import get_db
from app.models import Session, User
from app.ratelimit import client_ip
from app.security import hash_session_token

logger = logging.getLogger("app.deps")


def require_captcha(action: str) -> Callable[[Request], None]:
    """Valida el token de Turnstile de un endpoint PUBLICO. Se aplica con
    `dependencies=[Depends(require_captcha("login"))]` en el decorador de ruta,
    para no tocar la firma de ningun handler ni chocar con `@limiter.limit(...)`.

    `action` identifica el formulario de origen (max 32 chars, `[a-z0-9_-]`) y se
    compara contra la que devuelve Cloudflare: sin eso, un token sacado de
    `/recuperar` sirve para pegarle a `/signup`.

    Dos niveles de apagado, los dos deliberados:

    - Sin `TURNSTILE_SECRET_KEY` -> no-op total (ni siquiera sale a la red). Mismo
      fail-open que `origin_guard.py`: dev y local andan sin configurar nada.
    - Con la clave pero `TURNSTILE_ENFORCE=false` -> verifica y LOGUEA, pero nunca
      rechaza. Es la fase de observacion: sirve para medir cuantos requests
      legitimos fallarian antes de bloquear a nadie. Lo que hay que mirar en esos
      logs es `ok=False` con `missing-input-response`, que casi siempre significa
      que falta `NUXT_PUBLIC_TURNSTILE_SITE_KEY` en el build de ese entorno (se
      hornea en build: si falta, el front no manda token y no hay error visible).

    El detail del 403 es un codigo estable (`captcha_failed`) para que el front lo
    distinga de otros 403, igual que `legal_acceptance_required`."""

    def _dep(request: Request) -> None:
        if not settings.turnstile_secret_key:
            return

        ip = client_ip(request)
        result = turnstile.verify(
            request.headers.get("cf-turnstile-response"), ip, action
        )

        if not settings.turnstile_enforce:
            # Los fallos van en WARNING y los exitos en INFO A PROPOSITO: uvicorn
            # deja el root logger en WARNING, asi que un INFO no se ve en los logs
            # de Render — y el unico dato que importa de esta fase es justamente
            # cuantos requests LEGITIMOS fallarian al activar el enforcement.
            # Si esto fuera INFO, el modo observacion no observaria nada.
            if result.ok:
                logger.info("turnstile action=%s ok=True ip=%s (observacion)", action, ip)
            else:
                logger.warning(
                    "turnstile action=%s ok=False errors=%s ip=%s (observacion: NO se rechaza)",
                    action, result.error_codes, ip,
                )
            return

        if not result.ok:
            logger.warning(
                "turnstile action=%s RECHAZADO errors=%s ip=%s",
                action, result.error_codes, ip,
            )
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="captcha_failed")

    return _dep


def get_current_user(request: Request, db: DbSession = Depends(get_db)) -> User:
    """Lógica única de validación de sesión. Reutilizada por /auth/me y por
    cualquier endpoint protegido del dashboard."""
    token = request.cookies.get(settings.cookie_name)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="No autenticado")

    session = db.scalar(
        select(Session)
        .options(joinedload(Session.user))
        .where(Session.token_hash == hash_session_token(token))
    )
    if session is None or session.revoked_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sesion invalida")

    if session.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sesion expirada")

    user = session.user
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Usuario inactivo")

    return user


def require_legal_acceptance(user: User = Depends(get_current_user)) -> User:
    """Exige que el usuario tenga aceptada la version VIGENTE de los dos documentos
    legales. Drop-in de `get_current_user` en todo endpoint privado.

    Este 403 es el enforcement REAL. El middleware del front que manda a /politicas
    es solo UX: vive en el cliente, asi que un override de la respuesta de
    `/auth/me` (o directamente pegarle a la API con la cookie) lo saltea. Aca no hay
    nada que overridear — el estado se deriva en el servidor de la version guardada
    en DB (`User.terms_accepted` / `privacy_accepted`), no de nada que mande el
    cliente. Mismo criterio que el gate de `/exports/download`.

    Se dejan AFUERA a proposito: `/auth/me` (el front necesita leer los flags para
    saber a donde mandarlo), `/auth/logout` (poder salir siempre),
    `/auth/accept-legal` (seria un deadlock: no podria aceptar) y
    `/auth/change-password` (una clave temporal de admin se cambia primero).

    El detail es un codigo estable (`legal_acceptance_required`) para que el front
    lo distinga de otros 403 —cuenta paga, admin— igual que `premium_structure`."""
    if not (user.terms_accepted and user.privacy_accepted):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="legal_acceptance_required")
    return user


# require_admin y get_paid_user cuelgan de require_legal_acceptance (no de
# get_current_user) para que el gate se herede solo: cualquier endpoint admin o de
# cuenta paga queda cubierto sin tener que acordarse de sumarlo endpoint por
# endpoint. Los admins NO estan exentos: crean usuarios y ven pagos, con mas razon
# tienen que haber aceptado.
def require_admin(user: User = Depends(require_legal_acceptance)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Requiere admin")
    return user


def get_paid_user(
    user: User = Depends(require_legal_acceptance), db: DbSession = Depends(get_db)
) -> User:
    """Exige cuenta ilimitada (admin o tier pago vigente). Gate de las features
    pagas (ej. disenos guardados). Reusa la fuente unica de verdad de tiers."""
    from app.routers.tiers import user_is_unlimited

    if not user_is_unlimited(db, user, datetime.now(timezone.utc)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Requiere cuenta paga")
    return user

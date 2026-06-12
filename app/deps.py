from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.config import settings
from app.database import get_db
from app.models import Session, User
from app.security import hash_session_token


def get_current_user(request: Request, db: DbSession = Depends(get_db)) -> User:
    """Lógica única de validación de sesión. Reutilizada por /auth/me y por
    cualquier endpoint protegido del dashboard."""
    token = request.cookies.get(settings.cookie_name)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="No autenticado")

    session = db.scalar(
        select(Session).where(Session.token_hash == hash_session_token(token))
    )
    if session is None or session.revoked_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sesion invalida")

    if session.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sesion expirada")

    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Usuario inactivo")

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Requiere admin")
    return user

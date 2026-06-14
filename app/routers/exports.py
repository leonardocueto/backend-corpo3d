import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.database import get_db
from app.deps import get_current_user, require_admin
from app.models import ExportWindow, User
from app.schemas import ExportAttemptsOut, SetAttemptsIn

# Limite de exportaciones (Free Tier). Ventana ROLLING de 24h anclada al primer
# intento: el reset ocurre en window_start + WINDOW (hora del SERVIDOR, nunca la
# del cliente, para que cambiar el reloj local no de intentos extra).
router = APIRouter(prefix="/exports", tags=["exports"])

DAILY_LIMIT = 3
WINDOW = timedelta(hours=24)


def _admin_response() -> ExportAttemptsOut:
    """Los admin no tienen limite."""
    return ExportAttemptsOut(limit=DAILY_LIMIT, remaining=None, unlimited=True, reset_at=None)


def remaining_for(win: "ExportWindow | None", now: datetime) -> int:
    """Intentos efectivos de un usuario NO admin segun su ventana, considerando
    la expiracion de 24h (una ventana vencida/inexistente se cuenta como llena).
    Solo lectura: no crea ni resetea la fila."""
    if win is None or now >= win.window_start + WINDOW:
        return DAILY_LIMIT
    return win.remaining_attempts


def _get_or_create_locked(db: DbSession, user_id, now: datetime) -> ExportWindow:
    """Devuelve la fila del usuario con un lock de escritura (FOR UPDATE) para
    serializar clics simultaneos y evitar doble gasto. Si no existe, la crea;
    si dos requests la crean a la vez, una gana (unique en user_id) y la otra
    re-lee la fila ganadora bajo lock."""
    win = db.scalar(
        select(ExportWindow).where(ExportWindow.user_id == user_id).with_for_update()
    )
    if win is not None:
        return win

    win = ExportWindow(user_id=user_id, window_start=now, remaining_attempts=DAILY_LIMIT)
    db.add(win)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        win = db.scalar(
            select(ExportWindow).where(ExportWindow.user_id == user_id).with_for_update()
        )
    return win


@router.get("/attempts", response_model=ExportAttemptsOut)
def get_attempts(
    user: User = Depends(get_current_user), db: DbSession = Depends(get_db)
) -> ExportAttemptsOut:
    """Intentos restantes del usuario para la ventana actual. Solo lectura: no
    crea ni resetea la fila (una ventana expirada se reporta como fresca)."""
    if user.is_admin:
        return _admin_response()

    now = datetime.now(timezone.utc)
    win = db.scalar(select(ExportWindow).where(ExportWindow.user_id == user.id))
    active = win is not None and now < win.window_start + WINDOW
    return ExportAttemptsOut(
        limit=DAILY_LIMIT,
        remaining=remaining_for(win, now),
        unlimited=False,
        reset_at=win.window_start + WINDOW if active else None,
    )


@router.post("/attempts/use", response_model=ExportAttemptsOut)
def use_attempt(
    user: User = Depends(get_current_user), db: DbSession = Depends(get_db)
) -> ExportAttemptsOut:
    """Valida y consume un intento. Admin: ilimitado (no toca la tabla). Free:
    descuenta 1 si quedan; si no, 403. Fuente de verdad del contador."""
    if user.is_admin:
        return _admin_response()

    now = datetime.now(timezone.utc)
    win = _get_or_create_locked(db, user.id, now)

    # Ventana expirada -> arranca una nueva con el limite completo.
    if now >= win.window_start + WINDOW:
        win.window_start = now
        win.remaining_attempts = DAILY_LIMIT

    if win.remaining_attempts <= 0:
        # Sin cambios que persistir; el cierre de la sesion hace rollback.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Sin intentos de exportacion disponibles"
        )

    win.remaining_attempts -= 1
    db.commit()
    db.refresh(win)
    return ExportAttemptsOut(
        limit=DAILY_LIMIT,
        remaining=win.remaining_attempts,
        unlimited=False,
        reset_at=win.window_start + WINDOW,
    )


@router.put(
    "/attempts/{user_id}",
    response_model=ExportAttemptsOut,
    dependencies=[Depends(require_admin)],
)
def set_user_attempts(
    user_id: uuid.UUID, payload: SetAttemptsIn, db: DbSession = Depends(get_db)
) -> ExportAttemptsOut:
    """Carga manual de intentos a un usuario (admin). FIJA el contador a
    `amount` y reinicia la ventana de 24h. Solo para usuarios NO admin: a un
    admin no tiene sentido (es ilimitado) y abriria bugs en el contador."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")
    if target.is_admin:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="No se pueden cargar intentos a un admin (es ilimitado)",
        )

    now = datetime.now(timezone.utc)
    win = db.scalar(
        select(ExportWindow).where(ExportWindow.user_id == user_id).with_for_update()
    )
    if win is None:
        win = ExportWindow(user_id=user_id, window_start=now, remaining_attempts=payload.amount)
        db.add(win)
    else:
        win.window_start = now
        win.remaining_attempts = payload.amount

    db.commit()
    db.refresh(win)
    return ExportAttemptsOut(
        limit=DAILY_LIMIT,
        remaining=win.remaining_attempts,
        unlimited=False,
        reset_at=win.window_start + WINDOW,
    )

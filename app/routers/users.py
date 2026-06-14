"""CRUD de usuarios para el panel admin.

Todos los endpoints requieren admin (dependency a nivel router). La salida usa
`UserOut`, que NO expone `password_hash` ni datos sensibles.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.database import get_db
from app.deps import require_admin
from app.models import ExportWindow, User, UserTier
from app.routers.exports import remaining_for
from app.routers.tiers import PAID_TIERS, expiry_for, tier_is_unlimited
from app.schemas import (
    AdminUserOut,
    PasswordUpdate,
    UserCreate,
    UserOut,
    UsersPage,
    UserUpdate,
)
from app.security import hash_password

router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(require_admin)],  # todo el router es admin-only
)


@router.get("", response_model=UsersPage)
def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: DbSession = Depends(get_db),
):
    """Listado paginado de usuarios (mas nuevos primero), con sus intentos de
    exportacion actuales (admin = ilimitado)."""
    total = db.scalar(select(func.count()).select_from(User)) or 0
    rows = db.scalars(
        select(User)
        .order_by(User.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    # Ventanas y tiers de los usuarios de esta pagina en una query c/u (evita N+1).
    now = datetime.now(timezone.utc)
    ids = [u.id for u in rows]
    windows = (
        db.scalars(select(ExportWindow).where(ExportWindow.user_id.in_(ids))).all()
        if ids
        else []
    )
    tiers = (
        db.scalars(select(UserTier).where(UserTier.user_id.in_(ids))).all() if ids else []
    )
    win_by_user = {w.user_id: w for w in windows}
    tier_by_user = {t.user_id: t for t in tiers}

    def to_item(u: User) -> AdminUserOut:
        base = UserOut.model_validate(u).model_dump()
        tier_obj = tier_by_user.get(u.id)
        tier_name = tier_obj.tier if tier_obj else "free"
        unlimited = u.is_admin or tier_is_unlimited(tier_obj, now)
        return AdminUserOut(
            **base,
            tier=tier_name,
            tier_paid_at=tier_obj.paid_at if tier_obj else None,
            tier_expires_at=tier_obj.expires_at if tier_obj else None,
            export_remaining=None if unlimited else remaining_for(win_by_user.get(u.id), now),
            export_unlimited=unlimited,
        )

    return UsersPage(
        items=[to_item(u) for u in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: DbSession = Depends(get_db)):
    """Alta de usuario."""
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email ya registrado")
    user = User(
        email=payload.email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
    )
    db.add(user)
    db.flush()  # asigna user.id para el tier

    # Tier inicial: solo si es pago y NO admin (free queda lazy = 3 intentos;
    # admin es ilimitado por rol). Los intentos no se cargan a mano: free=3 por
    # defecto, pagos = ilimitado por el tier.
    if not user.is_admin and payload.tier in PAID_TIERS:
        now = datetime.now(timezone.utc)
        db.add(
            UserTier(
                user_id=user.id,
                tier=payload.tier,
                paid_at=now,
                expires_at=expiry_for(payload.tier, now),
            )
        )

    db.commit()
    db.refresh(user)
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    db: DbSession = Depends(get_db),
    current: User = Depends(require_admin),
):
    """Modifica email, nombre y/o acceso admin (parcial)."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")

    data = payload.model_dump(exclude_unset=True)

    # No permitir auto-quitarse el admin (evita quedarse sin ningun admin).
    if user_id == current.id and data.get("is_admin") is False:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="No podes quitarte tu propio admin")

    # Email unico (si cambia, chequear que no lo tenga otro usuario).
    new_email = data.get("email")
    if new_email and new_email != user.email:
        if db.scalar(select(User).where(User.email == new_email, User.id != user_id)):
            raise HTTPException(status.HTTP_409_CONFLICT, detail="Email ya registrado")

    for field, value in data.items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return user


@router.patch("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    user_id: uuid.UUID,
    payload: PasswordUpdate,
    db: DbSession = Depends(get_db),
):
    """Cambia/resetea la contraseña de un usuario."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")
    user.password_hash = hash_password(payload.password)
    db.commit()


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: uuid.UUID,
    db: DbSession = Depends(get_db),
    current: User = Depends(require_admin),
):
    """Elimina un usuario (sus sesiones caen por ON DELETE CASCADE)."""
    if user_id == current.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="No podes eliminar tu propio usuario")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")
    db.delete(user)
    db.commit()

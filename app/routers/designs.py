import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app import storage
from app.database import get_db
from app.deps import get_paid_user
from app.models import User, UserDesign
from app.schemas import (
    DesignDetailOut,
    DesignRenameIn,
    DesignSaveIn,
    DesignsPage,
    DesignSummaryOut,
)

# Biblioteca de disenos guardados (solo cuentas pagas, via get_paid_user). El
# JSON + la miniatura viven en R2 (app/storage.py); aca solo la metadata. Todo
# scopeado por user.id: un diseno de otro usuario responde 404 (no 403, para no
# filtrar su existencia).
router = APIRouter(prefix="/designs", tags=["designs"])

MAX_DESIGNS = 20


def _count(db: DbSession, user_id: uuid.UUID) -> int:
    return db.scalar(
        select(func.count()).select_from(UserDesign).where(UserDesign.user_id == user_id)
    ) or 0


def _get_owned(db: DbSession, user: User, design_id: uuid.UUID) -> UserDesign:
    row = db.get(UserDesign, design_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Diseno no encontrado")
    return row


def _summary(row: UserDesign) -> DesignSummaryOut:
    return DesignSummaryOut(
        id=row.id,
        name=row.name,
        thumbnail_url=storage.presign_get(row.thumb_key),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _detail(row: UserDesign, data: dict | None = None) -> DesignDetailOut:
    # `data` se pasa cuando ya lo tenemos en memoria (create/update) para evitar
    # un GET extra a R2; si no, se lee del bucket.
    return DesignDetailOut(
        id=row.id,
        name=row.name,
        thumbnail_url=storage.presign_get(row.thumb_key),
        created_at=row.created_at,
        updated_at=row.updated_at,
        data=data if data is not None else storage.read_json(row.json_key),
    )


@router.get("", response_model=DesignsPage)
def list_designs(
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    user: User = Depends(get_paid_user),
    db: DbSession = Depends(get_db),
) -> DesignsPage:
    total = _count(db, user.id)
    rows = db.scalars(
        select(UserDesign)
        .where(UserDesign.user_id == user.id)
        .order_by(UserDesign.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return DesignsPage(
        items=[_summary(r) for r in rows], total=total, page=page, page_size=page_size
    )


@router.post("", response_model=DesignDetailOut, status_code=status.HTTP_201_CREATED)
def create_design(
    payload: DesignSaveIn,
    user: User = Depends(get_paid_user),
    db: DbSession = Depends(get_db),
) -> DesignDetailOut:
    if _count(db, user.id) >= MAX_DESIGNS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Limite de {MAX_DESIGNS} disenos alcanzado",
        )

    now = datetime.now(timezone.utc)
    design_id = uuid.uuid4()
    json_key, thumb_key = storage.design_keys(user.id, design_id, payload.name, now)
    storage.put_design(json_key, thumb_key, payload.data, payload.thumbnail)

    row = UserDesign(
        id=design_id,
        user_id=user.id,
        name=payload.name,
        json_key=json_key,
        thumb_key=thumb_key,
        created_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _detail(row, data=payload.data)


@router.get("/{design_id}", response_model=DesignDetailOut)
def get_design(
    design_id: uuid.UUID,
    user: User = Depends(get_paid_user),
    db: DbSession = Depends(get_db),
) -> DesignDetailOut:
    return _detail(_get_owned(db, user, design_id))


@router.put("/{design_id}", response_model=DesignDetailOut)
def update_design(
    design_id: uuid.UUID,
    payload: DesignSaveIn,
    user: User = Depends(get_paid_user),
    db: DbSession = Depends(get_db),
) -> DesignDetailOut:
    """"Guardar" sobre un diseno existente: re-sube JSON + miniatura. Si cambio el
    nombre, la key (parte legible) cambia -> se escribe en la nueva y se borra la
    vieja (best-effort, despues de commitear la fila)."""
    row = _get_owned(db, user, design_id)

    new_json_key, new_thumb_key = storage.design_keys(
        user.id, row.id, payload.name, row.created_at
    )
    storage.put_design(new_json_key, new_thumb_key, payload.data, payload.thumbnail)

    old_keys = (row.json_key, row.thumb_key)
    row.name = payload.name
    row.json_key = new_json_key
    row.thumb_key = new_thumb_key
    db.commit()
    db.refresh(row)

    stale = [k for k in old_keys if k not in (new_json_key, new_thumb_key)]
    if stale:
        storage.delete_keys(*stale)
    return _detail(row, data=payload.data)


@router.patch("/{design_id}", response_model=DesignSummaryOut)
def rename_design(
    design_id: uuid.UUID,
    payload: DesignRenameIn,
    user: User = Depends(get_paid_user),
    db: DbSession = Depends(get_db),
) -> DesignSummaryOut:
    """Renombrar sin re-subir data. Como el nombre va en la key, mueve el objeto
    (copy + delete) si la key cambia."""
    row = _get_owned(db, user, design_id)
    new_json_key, new_thumb_key = storage.design_keys(
        user.id, row.id, payload.name, row.created_at
    )

    if (new_json_key, new_thumb_key) != (row.json_key, row.thumb_key):
        storage.copy_object(row.json_key, new_json_key)
        storage.copy_object(row.thumb_key, new_thumb_key)
        old_keys = (row.json_key, row.thumb_key)
        row.json_key = new_json_key
        row.thumb_key = new_thumb_key
        row.name = payload.name
        db.commit()
        db.refresh(row)
        storage.delete_keys(*old_keys)
    else:
        row.name = payload.name
        db.commit()
        db.refresh(row)
    return _summary(row)


@router.delete("/{design_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_design(
    design_id: uuid.UUID,
    user: User = Depends(get_paid_user),
    db: DbSession = Depends(get_db),
) -> None:
    """Borra la fila Y los objetos en R2 (el cascade de la DB no toca el bucket)."""
    row = _get_owned(db, user, design_id)
    keys = (row.json_key, row.thumb_key)
    db.delete(row)
    db.commit()
    storage.delete_keys(*keys)

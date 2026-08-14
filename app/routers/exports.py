import io
import json
import posixpath
import re
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFont

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.database import get_db
from app.deps import require_admin, require_legal_acceptance
from app.models import ExportLog, ExportWindow, Payment, User
from app.ratelimit import limiter
from app.routers.tiers import user_is_unlimited
from app.schemas import ExportAttemptsOut, SetAttemptsIn
from app.storage import put_export_log

# Limite de exportaciones (Free Tier). Ventana ROLLING de 24h anclada al primer
# intento: el reset ocurre en window_start + WINDOW (hora del SERVIDOR, nunca la
# del cliente, para que cambiar el reloj local no de intentos extra).
router = APIRouter(prefix="/exports", tags=["exports"])

DAILY_LIMIT = 3
WINDOW = timedelta(hours=24)

# Estructuras premium (solo los IDs; las definiciones geometricas viven en el
# front, que las necesita para el preview). El gate real de descarga es este:
# un free puede previsualizarlas pero /exports/download se las rechaza.
PREMIUM_STRUCTURE_IDS = {
    "snap-fit-pvc",
    "double-channel",
    "snap-lip",
    "deep-base",
    "neon-3d",
    "corporea-aluminio",
}

# Limites del payload de /exports/download. El entregable trae, ademas del
# archivo completo por parte (hasta 4 partes x 3 formatos + PNG + PDF), un
# archivo POR LETRA y parte (pieza-N.dxf/svg/stl) -> un texto largo genera
# facil >100 archivos. El tope de bytes acota la RAM del armado del ZIP (todo
# en memoria) y queda debajo del limite de upload de Cloudflare (100 MB).
DOWNLOAD_LIMIT = "10/minute"
MAX_EXPORT_FILES = 400
MAX_EXPORT_BYTES = 80 * 1024 * 1024
ALLOWED_EXPORT_EXTENSIONS = {".dxf", ".svg", ".stl", ".png", ".pdf"}


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
    user: User = Depends(require_legal_acceptance), db: DbSession = Depends(get_db)
) -> ExportAttemptsOut:
    """Intentos restantes del usuario para la ventana actual. Solo lectura: no
    crea ni resetea la fila (una ventana expirada se reporta como fresca)."""
    now = datetime.now(timezone.utc)
    if user_is_unlimited(db, user, now):
        return _admin_response()

    win = db.scalar(select(ExportWindow).where(ExportWindow.user_id == user.id))
    active = win is not None and now < win.window_start + WINDOW
    return ExportAttemptsOut(
        limit=DAILY_LIMIT,
        remaining=remaining_for(win, now),
        unlimited=False,
        reset_at=win.window_start + WINDOW if active else None,
    )


def _consume_attempt(db: DbSession, user_id, now: datetime) -> ExportWindow:
    """Valida y descuenta 1 intento bajo lock (FOR UPDATE), SIN commitear: el
    caller decide cuando confirmar. Asi /exports/download puede armar el ZIP con
    el debit pendiente y, si el armado falla, el rollback devuelve el intento.
    Ventana expirada -> arranca una nueva con el limite completo. Sin intentos ->
    403 (sin cambios que persistir; el cierre de la sesion hace rollback)."""
    win = _get_or_create_locked(db, user_id, now)

    if now >= win.window_start + WINDOW:
        win.window_start = now
        win.remaining_attempts = DAILY_LIMIT

    if win.remaining_attempts <= 0:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Sin intentos de exportacion disponibles"
        )

    win.remaining_attempts -= 1
    return win



WATERMARK_TEXT = "CorpoLab 3D"


def _watermark_png(data: bytes) -> bytes:
    """Estampa el watermark en un PNG para cuentas free. Estilo identico al del
    front (semi-transparente, bottom-right, 4% de la altura)."""
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    fs = round(img.height * 0.04)
    font = ImageFont.load_default(size=fs)
    draw.text(
        (img.width - fs, img.height - fs),
        WATERMARK_TEXT,
        fill=(255, 255, 255, 140),
        font=font,
        anchor="rb",
    )
    img = Image.alpha_composite(img, overlay)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _safe_zip_name(raw: str) -> str:
    """Nombre de archivo del ZIP saneado desde el nombre del proyecto."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", raw.strip()).strip("-") or "export"
    return f"{name[:80]}.zip"


def _validate_export_path(raw: str) -> str:
    """Path relativo del archivo dentro del ZIP, saneado. Rechaza traversal,
    absolutos y extensiones fuera de la whitelist (400)."""
    path = (raw or "").replace("\\", "/").strip()
    if not path or path.startswith("/") or ":" in path:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Path de archivo invalido")
    normalized = posixpath.normpath(path)
    parts = normalized.split("/")
    if normalized.startswith("/") or ".." in parts or normalized in {".", ""}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Path de archivo invalido")
    ext = posixpath.splitext(normalized)[1].lower()
    if ext not in ALLOWED_EXPORT_EXTENSIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Tipo de archivo no permitido")
    return normalized


@router.post("/download")
@limiter.limit(DOWNLOAD_LIMIT)
def download_export(
    request: Request,
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    structure_id: str = Form(...),
    project_name: str = Form(""),
    user: User = Depends(require_legal_acceptance),
    db: DbSession = Depends(get_db),
) -> Response:
    """Punto de enforcement REAL de la descarga: recibe los archivos generados en
    el cliente (DXF/SVG/STL/PNG/PDF con su path relativo como filename), valida
    server-side (estructura premium -> requiere cuenta paga; free -> descuenta 1
    intento) y devuelve el ZIP armado aca. La response ES el artefacto: un
    override de responses en devtools no puede fabricar el ZIP del diseno actual.
    El debit y la entrega son atomicos (commit recien con el ZIP armado).
    Registra un ExportLog de auditoria y sube el .txt a R2 en background."""
    now = datetime.now(timezone.utc)
    unlimited = user_is_unlimited(db, user, now)

    # Estructura premium: solo cuentas ilimitadas (detail distinguible del 403
    # de intentos para que el front abra el modal de upsell correcto).
    if structure_id in PREMIUM_STRUCTURE_IDS and not unlimited:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="premium_structure")

    # Sanitizar el payload ANTES de debitar (un 4xx aca no consume intento).
    if len(files) == 0 or len(files) > MAX_EXPORT_FILES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Cantidad de archivos invalida")

    entries: list[tuple[str, bytes]] = []
    total = 0
    seen: set[str] = set()
    for f in files:
        path = _validate_export_path(f.filename or "")
        if path in seen:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Path de archivo duplicado")
        seen.add(path)
        content = f.file.read()
        total += len(content)
        if total > MAX_EXPORT_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Export demasiado grande"
            )
        # La ficha tecnica PDF es feature paga: se filtra server-side (un free
        # con el flag `unlimited` forzado en el cliente tampoco la consigue).
        if path.lower().endswith(".pdf") and not unlimited:
            continue
        if path.lower().endswith(".png") and not unlimited:
            content = _watermark_png(content)
        entries.append((path, content))

    if not entries:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Export sin archivos")

    # Free: debitar bajo lock, sin commit todavia (atomico con el armado).
    if not unlimited:
        _consume_attempt(db, user.id, now)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in entries:
            zf.writestr(path, content)

    # Audit log: ultimo pago aprobado del usuario (best-effort, nullable).
    last_payment = db.scalar(
        select(Payment)
        .where(Payment.user_id == user.id, Payment.status == "approved")
        .order_by(Payment.created_at.desc())
        .limit(1)
    )
    downloaded_at = datetime.now(timezone.utc)
    log = ExportLog(
        user_id=user.id,
        payment_id=last_payment.id if last_payment else None,
        r2_key="",
    )
    db.add(log)
    db.flush()
    log.r2_key = f"exports/{user.id}/{log.id}.txt"

    log_content = json.dumps({
        "payment_id": str(last_payment.id) if last_payment else None,
        "user_id": str(user.id),
        "project_name": project_name or None,
        "structure_id": structure_id,
        "file_count": len(entries),
        "generated_at": now.isoformat(),
        "downloaded_at": downloaded_at.isoformat(),
    })
    background.add_task(put_export_log, log.r2_key, log_content)

    db.commit()
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{_safe_zip_name(project_name)}"'
        },
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
    `amount` y reinicia la ventana de 24h. Solo para usuarios con limite: a uno
    ilimitado (admin o tier pago vigente) no tiene sentido y abriria bugs."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")

    now = datetime.now(timezone.utc)
    if user_is_unlimited(db, target, now):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="No se pueden cargar intentos a un usuario ilimitado",
        )

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

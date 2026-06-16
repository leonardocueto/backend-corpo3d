"""Almacenamiento de disenos en Cloudflare R2 (S3-compatible).

El JSON del ProjectState y la miniatura JPEG NO viven en Postgres: van a un
bucket PRIVADO en R2. La tabla `user_designs` solo guarda metadata + las keys.
Las miniaturas se sirven al front via presigned GET URL (URL firmada con
expiracion); el JSON lo lee el backend y lo devuelve inline (queda detras de
`get_paid_user`, sin exponer URLs ni tocar CORS de R2 desde el front).

Estructura de keys (un folder por usuario, y dentro uno por diseno con el UUID
para que "Guardar" sobreescriba sin colisiones; `fecha-nombre` es la parte
legible):
    designs/{user_id}/{design_id}/{YYYYMMDD}-{slug-del-nombre}.json
    designs/{user_id}/{design_id}/{YYYYMMDD}-{slug-del-nombre}.jpg
"""

import base64
import json
import re
import uuid
from datetime import datetime
from functools import lru_cache

import boto3
from botocore.config import Config

from app.config import settings


@lru_cache
def _client():
    """Cliente S3 apuntando al endpoint de R2 (lazy + cacheado). Falla con un
    mensaje claro si faltan credenciales (la app arranca sin R2, pero /designs no
    puede operar sin configurarlo)."""
    if not all(
        (settings.r2_account_id, settings.r2_access_key_id,
         settings.r2_secret_access_key, settings.r2_bucket)
    ):
        raise RuntimeError(
            "Cloudflare R2 no configurado: faltan R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/"
            "R2_SECRET_ACCESS_KEY/R2_BUCKET en el entorno."
        )
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _slug(name: str) -> str:
    """Nombre seguro para una path: alfanumerico + . _ - ; el resto -> _."""
    s = _SLUG_RE.sub("_", name.strip()).strip("._-")
    return (s or "diseno")[:80]


def design_keys(
    user_id: uuid.UUID, design_id: uuid.UUID, name: str, created_at: datetime
) -> tuple[str, str]:
    """(json_key, thumb_key) para un diseno. La parte legible usa la FECHA DE
    CREACION (fija) + el nombre actual (puede cambiar al renombrar/guardar)."""
    base = f"designs/{user_id}/{design_id}/{created_at.strftime('%Y%m%d')}-{_slug(name)}"
    return f"{base}.json", f"{base}.jpg"


def _decode_data_url(data_url: str) -> bytes:
    """Bytes de un data URL base64 (data:image/jpeg;base64,...). Acepta tambien
    base64 pelado (sin el prefijo data:)."""
    if data_url.startswith("data:") and "," in data_url:
        data_url = data_url.split(",", 1)[1]
    return base64.b64decode(data_url)


def put_design(json_key: str, thumb_key: str, data: dict, thumbnail: str) -> None:
    c = _client()
    c.put_object(
        Bucket=settings.r2_bucket,
        Key=json_key,
        Body=json.dumps(data).encode("utf-8"),
        ContentType="application/json",
    )
    c.put_object(
        Bucket=settings.r2_bucket,
        Key=thumb_key,
        Body=_decode_data_url(thumbnail),
        ContentType="image/jpeg",
    )


def read_json(json_key: str) -> dict:
    obj = _client().get_object(Bucket=settings.r2_bucket, Key=json_key)
    return json.loads(obj["Body"].read())


def read_bytes(key: str) -> bytes:
    """Bytes crudos de un objeto (para que el backend proxee la miniatura: el
    navegador nunca toca R2 directo, todo pasa por endpoints rate-limited)."""
    obj = _client().get_object(Bucket=settings.r2_bucket, Key=key)
    return obj["Body"].read()


def copy_object(src_key: str, dst_key: str) -> None:
    """Copia server-side (R2/S3 no tiene 'rename'; renombrar = copy + delete)."""
    _client().copy_object(
        Bucket=settings.r2_bucket,
        CopySource={"Bucket": settings.r2_bucket, "Key": src_key},
        Key=dst_key,
    )


def delete_keys(*keys: str) -> None:
    c = _client()
    for key in keys:
        c.delete_object(Bucket=settings.r2_bucket, Key=key)

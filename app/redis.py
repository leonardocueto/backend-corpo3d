import logging
import sys

import redis

from app.config import settings

logger = logging.getLogger(__name__)


class RedisNotConfiguredError(RuntimeError):
    """Se pidio el cliente sin REDIS_URL seteada. Subclase de RuntimeError para no
    romper a quien ya lo capturaba asi; existe como clase propia para que `app.otp`
    la distinga de un RuntimeError cualquiera (que seria un bug, no falta de config)."""


_client: redis.Redis | None = None

if settings.redis_url:
    _client = redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
elif settings.otp_enabled:
    logger.critical("REDIS_URL is required when OTP_ENABLED=true")
    sys.exit(1)
else:
    # Sin OTP_ENABLED igual hace falta Redis para el login de admins (siempre pasan
    # por el 2do factor). No se mata el proceso aca porque los Render Cron Jobs
    # importan app.config/app.models sin REDIS_URL y no la necesitan.
    logger.warning(
        "REDIS_URL no configurada: el login de usuarios admin respondera 503 "
        "hasta que se configure (los admins requieren OTP siempre)."
    )


def get_redis() -> redis.Redis:
    if _client is None:
        raise RedisNotConfiguredError("Redis not configured")
    return _client


def ping_redis() -> bool:
    if _client is None:
        return False
    try:
        return _client.ping()
    except redis.RedisError:
        return False

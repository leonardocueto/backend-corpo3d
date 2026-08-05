import logging
import sys

import redis

from app.config import settings

logger = logging.getLogger(__name__)

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


def get_redis() -> redis.Redis:
    if _client is None:
        raise RuntimeError("Redis not configured")
    return _client


def ping_redis() -> bool:
    if _client is None:
        return False
    try:
        return _client.ping()
    except redis.RedisError:
        return False

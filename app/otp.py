import logging
import uuid

import redis as _redis

from app.config import settings
from app.redis import RedisNotConfiguredError, get_redis

logger = logging.getLogger(__name__)


class RedisUnavailableError(Exception):
    pass


def _key(user_id: uuid.UUID) -> str:
    return f"otp:{user_id}"


def store_otp(user_id: uuid.UUID, code_hash: str) -> None:
    try:
        r = get_redis()
        key = _key(user_id)
        pipe = r.pipeline()
        pipe.delete(key)
        pipe.hset(key, mapping={"code_hash": code_hash, "attempts": "0"})
        pipe.expire(key, settings.otp_minutes * 60)
        pipe.execute()
    except (_redis.RedisError, RedisNotConfiguredError) as exc:
        logger.error("Redis error in store_otp: %s", exc)
        raise RedisUnavailableError from exc


def verify_otp(user_id: uuid.UUID, code_hash: str) -> bool:
    try:
        r = get_redis()
        key = _key(user_id)
        data = r.hgetall(key)
        if not data:
            return False
        if int(data["attempts"]) >= settings.otp_max_attempts:
            return False
        if data["code_hash"] != code_hash:
            new_attempts = r.hincrby(key, "attempts", 1)
            if new_attempts >= settings.otp_max_attempts:
                r.delete(key)
            return False
        r.delete(key)
        return True
    except (_redis.RedisError, RedisNotConfiguredError) as exc:
        logger.error("Redis error in verify_otp: %s", exc)
        raise RedisUnavailableError from exc


def has_active_otp(user_id: uuid.UUID) -> bool:
    try:
        r = get_redis()
        return bool(r.exists(_key(user_id)))
    except (_redis.RedisError, RedisNotConfiguredError) as exc:
        logger.error("Redis error in has_active_otp: %s", exc)
        raise RedisUnavailableError from exc

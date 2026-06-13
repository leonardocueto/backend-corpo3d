import hashlib
import hmac
import secrets

from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def generate_token() -> str:
    # ~43 chars, 256 bits de entropía. Valor PLANO; nunca se guarda en DB tal cual.
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    # HMAC-SHA256 con pepper del servidor: aunque se filtre la DB, sin SESSION_SECRET
    # no se puede derivar/forjar un token válido. hex => 64 chars.
    return hmac.new(
        settings.session_secret.encode(), token.encode(), hashlib.sha256
    ).hexdigest()


# Alias historicos de la sesion (mismo mecanismo). El token de reset de password
# usa generate_token / hash_token directamente.
def generate_session_token() -> str:
    return generate_token()


def hash_session_token(token: str) -> str:
    return hash_token(token)

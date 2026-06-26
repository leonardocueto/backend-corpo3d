"""Verificacion del ID token de Google (OAuth 2.0 / OIDC).

Aislado del router: toda la dependencia de `google-auth` vive aca. La regla de oro
es la misma que el resto del backend: la identidad se valida SOLO en el servidor.
Nunca se confia en el `credential` que manda el front hasta verificarlo (firma con
los certs publicos de Google + `aud` == nuestro Client ID + `iss` + expiracion).
"""

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.config import settings


class GoogleAuthError(Exception):
    """El ID token no es valido (firma/aud/iss/exp) o Google no esta configurado."""


def verify_google_id_token(credential: str) -> dict:
    """Verifica el ID token y devuelve el payload (`sub`, `email`, `email_verified`,
    `name`, ...). Lanza GoogleAuthError si es invalido o si falta el Client ID.

    `verify_oauth2_token` valida firma + `aud` (contra google_client_id) + `iss` +
    expiracion; si algo falla lanza ValueError, que envolvemos en GoogleAuthError."""
    if not settings.google_client_id:
        raise GoogleAuthError("Login con Google no configurado (falta GOOGLE_CLIENT_ID)")
    try:
        return id_token.verify_oauth2_token(
            credential, google_requests.Request(), settings.google_client_id
        )
    except ValueError as exc:  # token invalido/expirado/aud incorrecta
        raise GoogleAuthError(str(exc)) from exc

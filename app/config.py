import json
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"            # "production" activa cookie Secure
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/dashboard"
    session_secret: str                         # pepper para el HMAC del token (obligatorio)

    # Cookie de sesión
    cookie_name: str = "session_token"
    cookie_samesite: str = "lax"                # "lax" | "strict" | "none"
    cookie_domain: str | None = None
    session_days: int = 7

    # CORS. Acepta JSON (["https://a","https://b"]) o lista separada por comas
    # (https://a,https://b). NoDecode desactiva el parseo JSON automatico de
    # pydantic-settings para que el validator de abajo maneje ambos formatos.
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    # Regex opcional de origenes permitidos (override manual). Si se setea, manda.
    cors_origin_regex: str | None = None

    @field_validator("database_url", mode="before")
    @classmethod
    def _clean_database_url(cls, v: object) -> object:
        # Quita espacios/saltos de linea internos (una URL de DB valida no los
        # tiene sin encodear). Robustez ante copy-paste con wrap, ej. el clasico
        # "sslmode=requi re" que rompe psycopg.
        if isinstance(v, str):
            return "".join(v.split())
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v: object) -> object:
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            items = json.loads(s) if s.startswith("[") else s.split(",")  # JSON o comas
        elif isinstance(v, (list, tuple)):
            items = list(v)
        else:
            return v
        # Normaliza cada origen: sin espacios y SIN barra final (el header Origin
        # del navegador es scheme://host[:port], sin "/" ni path).
        return [str(o).strip().rstrip("/") for o in items if str(o).strip()]

    @property
    def cookie_secure(self) -> bool:
        # Secure obligatorio en prod; obligatorio también si SameSite=None
        return self.environment == "production" or self.cookie_samesite.lower() == "none"

    @property
    def effective_cors_origin_regex(self) -> str | None:
        # Si hay regex explicito, se respeta. Si no, en desarrollo permitimos
        # CUALQUIER puerto de localhost/127.0.0.1 (Nuxt puede arrancar en 3000,
        # 3001, etc.). En produccion no se asume nada: solo cors_origins exactos.
        if self.cors_origin_regex:
            return self.cors_origin_regex
        if self.environment != "production":
            return r"http://(localhost|127\.0\.0\.1):\d+"
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # CORS (JSON array en el .env)
    cors_origins: list[str] = ["http://localhost:3000"]
    # Regex opcional de origenes permitidos (override manual). Si se setea, manda.
    cors_origin_regex: str | None = None

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

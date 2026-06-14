import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None
    is_admin: bool = False


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str | None
    is_admin: bool


# --- Gestion de usuarios (panel admin) ---


class UserCreate(BaseModel):
    """Alta de usuario desde el panel."""

    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str | None = None
    is_admin: bool = False


class UserUpdate(BaseModel):
    """Modificacion parcial (PATCH). Solo se actualizan los campos enviados."""

    email: EmailStr | None = None
    full_name: str | None = None
    is_admin: bool | None = None


class PasswordUpdate(BaseModel):
    """Cambio/reseteo de contraseña."""

    password: str = Field(min_length=8)


class ForgotPasswordIn(BaseModel):
    """Pedido de link de recuperacion (publico, sin sesion)."""

    email: EmailStr


class ResetPasswordIn(BaseModel):
    """Reseteo con el token recibido por email."""

    token: str
    password: str = Field(min_length=8)


class AdminUserOut(UserOut):
    """Usuario en el listado admin: agrega los intentos de exportacion actuales.
    `export_remaining` es null y `export_unlimited` True para admin (ilimitado)."""

    export_remaining: int | None
    export_unlimited: bool


class UsersPage(BaseModel):
    """Pagina de resultados para el listado de usuarios."""

    items: list[AdminUserOut]
    total: int
    page: int
    page_size: int


# --- Limite de exportaciones (Free Tier) ---


class ExportAttemptsOut(BaseModel):
    """Estado del limite de exportaciones del usuario para la ventana actual.

    `remaining` es null y `unlimited` True para admin (sin limite). `reset_at`
    es la hora (del servidor) en que la ventana de 24h vuelve a tener los 3."""

    limit: int
    remaining: int | None
    unlimited: bool
    reset_at: datetime | None


class SetAttemptsIn(BaseModel):
    """Carga manual de intentos a un usuario (panel admin). FIJA el contador a
    `amount` y abre una ventana fresca de 24h."""

    amount: int = Field(ge=0, le=999)

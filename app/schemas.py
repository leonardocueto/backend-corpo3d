import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None
    is_admin: bool = False


class SignupIn(BaseModel):
    """Alta self-serve (endpoint publico). A diferencia de RegisterIn (admin),
    NO acepta `is_admin` ni `tier`: el endpoint fuerza siempre usuario comun +
    tier free. `password` con minimo 8 (igual que UserCreate y change-password)."""

    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str | None = None


class GoogleAuthIn(BaseModel):
    """Login con Google. `credential` es el ID token (JWT) que devuelve Google
    Identity Services en el front. Se verifica SOLO en el backend (firma + aud +
    exp + email_verified). La salida sigue siendo UserOut (sin campos sensibles)."""

    credential: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str | None
    is_admin: bool


class LoginResponse(BaseModel):
    """Respuesta del paso 1 del login. Dos casos segun `OTP_ENABLED`:
    - OTP ON:  `otp_required=True`, `user=None` -> falta verificar el codigo por email.
    - OTP OFF: `otp_required=False`, `user=<UserOut>` -> la sesion ya quedo iniciada
      (cookie seteada), igual que el login de un solo paso."""

    otp_required: bool = True
    user: UserOut | None = None


class VerifyOtpIn(BaseModel):
    """Verificacion del codigo OTP recibido por email (2do paso del login)."""

    email: EmailStr
    code: str = Field(min_length=6, max_length=6)


class ResendOtpIn(BaseModel):
    """Reenvio del codigo OTP (boton "reenviar" de la pantalla de verificacion)."""

    email: EmailStr


# --- Gestion de usuarios (panel admin) ---


class UserCreate(BaseModel):
    """Alta de usuario desde el panel. `tier` se ignora si es admin."""

    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str | None = None
    is_admin: bool = False
    tier: Literal["free", "mensual", "anual"] = "free"


class UserUpdate(BaseModel):
    """Modificacion parcial (PATCH). Solo se actualizan los campos enviados."""

    email: EmailStr | None = None
    full_name: str | None = None
    is_admin: bool | None = None


class PasswordUpdate(BaseModel):
    """Cambio/reseteo de contraseña."""

    password: str = Field(min_length=8)


class ChangePasswordIn(BaseModel):
    """Cambio de la propia contraseña (usuario con sesion activa)."""

    current_password: str
    new_password: str = Field(min_length=8)


class ForgotPasswordIn(BaseModel):
    """Pedido de link de recuperacion (publico, sin sesion)."""

    email: EmailStr


class ResetPasswordIn(BaseModel):
    """Reseteo con el token recibido por email."""

    token: str
    password: str = Field(min_length=8)


class AdminUserOut(UserOut):
    """Usuario en el listado admin: agrega tier e intentos de exportacion.
    `export_remaining` es null y `export_unlimited` True para usuarios ilimitados
    (admin o tier pago vigente). `tier_expires_at` es el vto del tier pago."""

    tier: str
    tier_paid_at: datetime | None
    tier_expires_at: datetime | None
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


# --- Tiers de usuario ---


class SetTierIn(BaseModel):
    """Asignacion de tier desde el panel admin."""

    tier: Literal["free", "mensual", "anual"]


class UserTierOut(BaseModel):
    """Tier actual de un usuario."""

    tier: str
    paid_at: datetime | None
    expires_at: datetime | None


# --- Disenos guardados (cuentas pagas) ---


class DesignSaveIn(BaseModel):
    """Crear (POST) o sobreescribir (PUT) un diseno. `thumbnail` es un data URL
    JPEG que captura el front; el backend lo decodifica y lo sube a R2 (NO se
    persiste en DB). El nombre no-vacio se valida en el router (trim) para devolver
    un error claro y consistente (un name solo-espacios pasaria min_length)."""

    name: str = Field(max_length=255)
    data: dict
    thumbnail: str


class DesignRenameIn(BaseModel):
    """Renombrar sin re-subir data (PATCH)."""

    name: str = Field(max_length=255)


class DesignSummaryOut(BaseModel):
    """Item del listado (liviano): SIN el JSON del diseno ni las keys internas.
    La miniatura se pide aparte a `GET /designs/{id}/thumbnail` (proxeada por el
    backend; el front arma la URL con id + updated_at como cache-buster)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime


class DesignDetailOut(DesignSummaryOut):
    """Detalle para abrir en el editor: agrega el ProjectState completo (leido de R2)."""

    data: dict


class DesignsPage(BaseModel):
    """Pagina de resultados para el listado de disenos."""

    items: list[DesignSummaryOut]
    total: int
    page: int
    page_size: int

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


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
    tier free. `password` con minimo 8 (igual que UserCreate y change-password).

    Tampoco acepta las VERSIONES de los legales: las estampa el servidor
    (`settings.terms_version` / `privacy_version`). Un string de version que viene
    del navegador no prueba nada."""

    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str | None = None
    # Aceptacion de cada documento, por separado. Default False A PROPOSITO
    # (fail-closed): un payload que no manda el campo FALLA con 422, no pasa de
    # largo dando el consentimiento por supuesto.
    # `validate_default=True` es IMPRESCINDIBLE: pydantic v2 NO corre los validators
    # sobre los defaults. Sin esto, omitir el campo se saltea el validator y el alta
    # entra sin consentimiento (probado: respondia 202).
    accepted_terms: bool = Field(default=False, validate_default=True)
    accepted_privacy: bool = Field(default=False, validate_default=True)

    @field_validator("accepted_terms")
    @classmethod
    def _must_accept_terms(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Tenes que aceptar los Terminos y Condiciones")
        return v

    @field_validator("accepted_privacy")
    @classmethod
    def _must_accept_privacy(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Tenes que aceptar las Politicas de Privacidad")
        return v


class AcceptLegalIn(BaseModel):
    """Aceptacion de los legales desde /politicas (usuario con sesion activa que
    todavia no acepto, o que tiene una version vieja). Se manda que documento se
    esta aceptando; el servidor sella la VERSION vigente de cada uno.

    Los dos default False: aceptar es un acto explicito. Mandar `false` no es un
    error (no rompe con 422) — simplemente no sella ese documento, y el usuario
    sigue bloqueado hasta que lo acepte."""

    accept_terms: bool = False
    accept_privacy: bool = False


class VerifySignupIn(BaseModel):
    """Confirmacion del alta (double opt-in): consume el token del email y crea la
    cuenta real. Espejo del `token` de ResetPasswordIn."""

    token: str


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
    # True = la clave es una temporal puesta por un admin. El front lo usa para
    # forzar el paso por /cambiar-password antes de dejar entrar a la app.
    must_change_password: bool = False
    # Estado de aceptacion de cada documento legal, DERIVADO en el servidor
    # (`User.terms_accepted` / `privacy_accepted`: version guardada == vigente).
    # El front los usa para mandar a /politicas y para decidir que checkbox mostrar.
    # NO son enforcement: un cliente puede mentirle a su propio front. Lo que corta
    # de verdad es `require_legal_acceptance` (403) en los endpoints privados.
    terms_accepted: bool = False
    privacy_accepted: bool = False


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
    # El panel lo manda en True cuando la clave salio de su generador: es una clave
    # nuestra, temporal, asi que al usuario se le pide cambiarla al ingresar.
    must_change_password: bool = False


class UserUpdate(BaseModel):
    """Modificacion parcial (PATCH). Solo se actualizan los campos enviados."""

    email: EmailStr | None = None
    full_name: str | None = None
    is_admin: bool | None = None


class PasswordUpdate(BaseModel):
    """Cambio/reseteo de contraseña desde el panel admin. `must_change_password`
    marca la clave como temporal (la genero el panel): el usuario tiene que
    cambiarla en el primer ingreso."""

    password: str = Field(min_length=8)
    must_change_password: bool = False


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
    # Evidencia de aceptacion (fecha + version aceptada de cada documento). Van aca
    # y NO en UserOut, que lleva solo los bools derivados: la evidencia cruda es
    # para el panel admin. None = no consta (alta hecha por un admin).
    terms_accepted_at: datetime | None = None
    terms_version: str | None = None
    privacy_accepted_at: datetime | None = None
    privacy_version: str | None = None


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


# --- Pagos (Mercado Pago) ---


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID | None
    user_email: str | None
    user_full_name: str | None
    plan: str
    status: str
    amount: int
    mp_payment_id: str | None
    subscription_id: uuid.UUID | None
    created_at: datetime


class PaymentsPage(BaseModel):
    items: list[PaymentOut]
    total: int
    page: int
    page_size: int
    currency: str


# --- Suscripciones (Mercado Pago Preapproval) ---


class SubscribeIn(BaseModel):
    plan: Literal["mensual", "anual"]


class SubscribeOut(BaseModel):
    init_point: str


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    plan: str
    status: str
    created_at: datetime


class CancelSubscriptionOut(BaseModel):
    status: str


class WithdrawalRequestIn(BaseModel):
    """Solicitud de arrepentimiento (Ley 24.240 art. 34)."""

    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    # Opcional a proposito: el art. 34 permite revocar SIN justificar. Pedirlo como
    # requisito bloquearia el ejercicio del derecho.
    reason: str | None = Field(default=None, max_length=2000)

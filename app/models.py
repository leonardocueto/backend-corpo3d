import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    # Nullable: un usuario creado por Google no tiene password (login solo OAuth).
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # True = la clave actual es TEMPORAL: la genero un admin desde el panel y se la
    # mando al usuario por mail. El front (middleware global) no lo deja salir de
    # /cambiar-password hasta que elija una propia. La bajan change-password y
    # reset-password. Nunca se prende sola: solo la setea el panel admin.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # `sub` estable de la cuenta de Google (id de la identidad). Unico; linkea
    # de forma robusta aunque cambie el email. Null = cuenta solo email/password.
    google_sub: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    # Como se dio de alta / con que metodo entra: 'password' | 'google'. Solo
    # informativo (panel admin); no cambia el mecanismo de sesion.
    auth_provider: Mapped[str] = mapped_column(
        String(16), default="password", server_default="password", nullable=False
    )
    # Aceptacion de los legales, UNA POR DOCUMENTO. Es la EVIDENCIA de que el
    # usuario vio el contrato: sin esto, clausulas como la validacion de fabricacion
    # o el limite de responsabilidad son dificiles de oponer. Se sella en el signup
    # (el momento del clic real, no el de la confirmacion del mail) y viaja hasta
    # aca por `PendingRegistration`, o en /auth/accept-legal para los que ya tenian
    # cuenta. Se guarda la VERSION aceptada, no un bool: es lo que permite pedir la
    # re-aceptacion cuando el documento cambia.
    # NULL = no consta (altas por admin). 'legacy-backfill' = cuenta anterior al
    # checkbox, dada por aceptada por uso previo (migracion 0014), NO un clic real.
    terms_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    terms_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    privacy_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    privacy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Estado de aceptacion DERIVADO: se compara la version guardada contra la
    # vigente. No es una columna bool a proposito — asi, cuando sube la version de
    # un documento, el flag se apaga solo y el usuario tiene que volver a aceptar,
    # sin backfill ni job. Un valor viejo ('legacy-backfill' o la version anterior)
    # da False.
    # Es la UNICA definicion de "acepto": la usan UserOut (lo que ve el front) y
    # `require_legal_acceptance` (el 403 que hace de enforcement real). Que sean la
    # misma fuente es lo que impide que el front y el backend opinen distinto.
    @property
    def terms_accepted(self) -> bool:
        from app.config import settings

        return self.terms_version == settings.terms_version

    @property
    def privacy_accepted(self) -> bool:
        from app.config import settings

        return self.privacy_version == settings.privacy_version

    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    designs: Mapped[list["UserDesign"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="sessions")


class PasswordResetToken(Base):
    """Token de un solo uso para resetear contraseña. Mismo principio que Session:
    en DB vive SOLO el HMAC del token; el valor plano viaja por email al usuario.
    Single-use (`used_at`) y de vida corta (`expires_at`)."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship()


class PendingRegistration(Base):
    """Alta self-serve a la espera de confirmar el email (double opt-in). Mismo
    principio que PasswordResetToken: en DB vive SOLO el HMAC del token; el valor
    plano viaja por email al usuario. La CUENTA todavia no existe: aca se guardan
    los datos del alta (email + full_name + password ya hasheado) y recien se crea
    el `User` real cuando se consume el token en `/auth/verify-signup`.

    `email` va indexado pero NO unico: se reusa tras confirmar/expirar, y un nuevo
    signup invalida los pendientes previos del mismo email (un solo link vivo a la
    vez). Single-use (`used_at`) y de vida corta (`expires_at`)."""

    __tablename__ = "pending_registrations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Aceptacion de los legales: ocurre ACA (el usuario tildo los checkboxes y mando
    # el form), pero el `User` recien nace al confirmar el mail. Se copian tal cual
    # en /auth/verify-signup, igual que `password_hash`: lo que vale es el momento
    # del clic, no el de la confirmacion.
    terms_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    terms_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    privacy_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    privacy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)


class LoginOtp(Base):
    """Codigo OTP de un solo uso para el 2do factor del login por email. Mismo
    principio que PasswordResetToken: en DB vive SOLO el HMAC del codigo; el codigo
    plano (6 digitos) viaja por email. Single-use (`used_at`), de vida corta
    (`expires_at`) y con tope de `attempts` (el codigo numerico es de baja entropia,
    asi que se invalida tras pocos intentos fallidos)."""

    __tablename__ = "login_otps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship()


class ExportWindow(Base):
    """Ventana rolling de 24h del limite de exportaciones por usuario (Free Tier).

    Una sola fila por usuario (`user_id` unico): se reutiliza/resetea, no es
    historico. `window_start` ancla la ventana actual; la ventana sigue viva
    hasta `window_start + 24h` (calculado contra la hora del SERVIDOR, nunca la
    del cliente). Los admin no tienen fila: su limite es ilimitado."""

    __tablename__ = "export_windows"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    remaining_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship()


class UserDesign(Base):
    """Diseno guardado por un usuario pago. El JSON del ProjectState y la
    miniatura JPEG viven en Cloudflare R2 (bucket privado); aca solo va la
    metadata + las keys del objeto. Varias filas por usuario (indexed, NO unique).

    OJO: el cascade de la DB borra la FILA, no el objeto en R2; al borrar diseno o
    usuario hay que limpiar el bucket explicitamente (ver routers/designs.py)."""

    __tablename__ = "user_designs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    json_key: Mapped[str] = mapped_column(String(512), nullable=False)
    thumb_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="designs")


class Subscription(Base):
    """Suscripcion recurrente de Mercado Pago (Preapproval API). Multiples filas
    por usuario (puede cancelar y re-suscribirse). `mp_preapproval_id` UNIQUE
    es la clave de idempotencia para el webhook `subscription_preapproval`."""

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    plan: Mapped[str] = mapped_column(String(16), nullable=False)
    mp_preapproval_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    mp_status: Mapped[str] = mapped_column(String(16), nullable=False)
    mp_payer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship()


class UserTier(Base):
    """Tier de un usuario. Una fila por usuario (`user_id` unico); sin fila se
    trata como `free`. Autoridad dual: suscripcion activa (primario) +
    `expires_at` como safety net (fallback para legacy one-time y webhook que
    no llega)."""

    __tablename__ = "user_tiers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    tier: Mapped[str] = mapped_column(String(16), default="free", nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    expiry_warning_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship()


class Payment(Base):
    """Pago de Mercado Pago (one-time o suscripcion). Sirve de auditoria y de
    garantia de IDEMPOTENCIA (`mp_payment_id` UNIQUE). Pagos de suscripcion
    referencian su `Subscription` via `subscription_id`; pagos legacy one-time
    tienen `subscription_id = NULL`."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    plan: Mapped[str] = mapped_column(String(16), nullable=False)
    mp_payment_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User | None"] = relationship()


class ExportLog(Base):
    """Registro de auditoria de una descarga exitosa. El archivo .txt vive en
    R2 (key en `r2_key`); esta fila es la fuente de verdad para el cleanup
    cron (borra logs > 6 meses). Solo visible para admin."""

    __tablename__ = "export_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL"), index=True, nullable=True
    )
    r2_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )

    user: Mapped["User | None"] = relationship()


class WithdrawalRequest(Base):
    """Solicitud de arrepentimiento (Ley 24.240 art. 34 + Res. 424/2020).

    ESTA FILA ES EL REGISTRO LEGAL; el mail a info@ es solo la notificacion.
    El orden importa y esta fijado en `POST /payments/withdrawal`: se commitea
    ANTES de encolar el mail. Antes de esta tabla el endpoint solo mandaba el
    mail, y como `_send` (app/email.py) traga los errores por diseno, una caida
    de Resend dejaba al cliente con un 204 de exito y la solicitud sin rastro en
    ningun lado. Con un plazo legal de 10 dias corridos, perderla no es un mail
    perdido: es un incumplimiento que ademas no se puede ni auditar.

    Sin FK a `users` a proposito (mismo criterio que PendingRegistration): el
    endpoint es PUBLICO y quien revoca puede no tener sesion abierta. `email`
    va indexado pero NO unico (se puede pedir mas de una vez).

    `resolved_at` lo estampa un admin desde el panel cuando la solicitud quedo
    gestionada (reembolso + cancelacion, ambos manuales en el panel de MP)."""

    __tablename__ = "withdrawal_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    # Nullable a proposito: el art. 34 permite revocar SIN justificar.
    reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

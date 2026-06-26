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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

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


class UserTier(Base):
    """Tier de un usuario. Una fila por usuario (`user_id` unico); sin fila se
    trata como `free`. Los tier pagos (`mensual`/`anual`) dan exportaciones
    ilimitadas mientras `expires_at` no haya vencido (se evalua contra la hora
    del SERVIDOR al leer; un pago vencido revierte a free solo)."""

    __tablename__ = "user_tiers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    tier: Mapped[str] = mapped_column(String(16), default="free", nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship()


class Payment(Base):
    """Pago de Mercado Pago (Checkout Pro / pago unico) que activo un tier. Sirve
    de auditoria y, sobre todo, de garantia de IDEMPOTENCIA: MP reintenta el
    webhook varias veces y `mp_payment_id` UNIQUE evita activar/duplicar dos veces
    el mismo pago. Varias filas por usuario (cada compra/renovacion es una)."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    plan: Mapped[str] = mapped_column(String(16), nullable=False)
    # ID del pago en Mercado Pago. Unico (idempotencia). Nullable por si se quiere
    # registrar un intento antes de conocerlo (hoy se crea ya con el id real).
    mp_payment_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship()

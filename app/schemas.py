import uuid

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


class UsersPage(BaseModel):
    """Pagina de resultados para el listado de usuarios."""

    items: list[UserOut]
    total: int
    page: int
    page_size: int

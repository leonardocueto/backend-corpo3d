"""Crea el primer usuario admin (bootstrap). Uso: python -m scripts.create_admin"""
import sys
from getpass import getpass

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select

from app.database import SessionLocal
from app.models import User
from app.security import hash_password

# Mismo validador de formato de email que usan los endpoints (EmailStr de Pydantic).
_email_validator = TypeAdapter(EmailStr)


def main() -> None:
    email = input("Email admin: ").strip()
    try:
        _email_validator.validate_python(email)
    except ValidationError:
        print("Email invalido: debe tener formato de correo (ej. nombre@dominio.com).")
        sys.exit(1)
    password = getpass("Password: ")
    full_name = input("Nombre (opcional): ").strip() or None

    db = SessionLocal()
    try:
        if db.scalar(select(User).where(User.email == email)):
            print("Ya existe un usuario con ese email.")
            sys.exit(1)
        db.add(
            User(
                email=email,
                full_name=full_name,
                password_hash=hash_password(password),
                is_admin=True,
                is_active=True,
            )
        )
        db.commit()
        print(f"Admin creado: {email}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

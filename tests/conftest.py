"""Infraestructura de tests: base efimera por corrida + transaccion por test.

Requiere `TEST_DATABASE_URL`, que apunta a la base de MANTENIMIENTO creada por
`tests/setup_test_db.sql` (rol restringido, base `*_test`, host local). Sin esa
variable la corrida aborta: nunca se cae a `DATABASE_URL`, que en una maquina de
desarrollo puede ser cualquier cosa.

Aislamiento entre tests, aunque los endpoints hagan `commit()`:
cada test abre una conexion, empieza una transaccion EXTERNA y le da a la app una
sesion con `join_transaction_mode="create_savepoint"`. Asi el `db.commit()` de un
endpoint solo libera un savepoint, y al terminar el test se deshace la
transaccion externa entera. `test_isolation.py` verifica que esto funcione.
"""

import os

import pytest

from tests._dbs import app_env, create_database, drop_database, run_alembic
from tests._safety import assert_not_production, assert_safe_url

MAINTENANCE_URL = os.environ.get("TEST_DATABASE_URL", "")
_RUN_URL: str | None = None


def pytest_configure(config):
    """Crea la base de la corrida y la lleva a `head` ANTES de importar la app:
    `app.database` crea el engine al importarse, con la URL que haya en el
    entorno en ese momento."""
    global _RUN_URL
    assert_not_production()
    if not MAINTENANCE_URL:
        pytest.exit(
            "Falta TEST_DATABASE_URL (ver tests/setup_test_db.sql y CLAUDE.md -> Tests).",
            returncode=2,
        )
    assert_safe_url(MAINTENANCE_URL)
    _RUN_URL = create_database(MAINTENANCE_URL, "run")
    os.environ.update(app_env(_RUN_URL))
    run_alembic(_RUN_URL, "upgrade", "head")


def pytest_unconfigure(config):
    if _RUN_URL is None:
        return
    from app.database import engine

    engine.dispose()
    drop_database(MAINTENANCE_URL, _RUN_URL)


@pytest.fixture(scope="session", autouse=True)
def _app_under_test():
    """Ultima linea de defensa: la app quedo configurada con la base efimera y sin
    ningun servicio externo, aunque haya un `.env` con valores reales."""
    from app.config import settings
    from app.ratelimit import limiter

    assert settings.database_url == _RUN_URL
    for name in (
        "resend_api_key",
        "turnstile_secret_key",
        "origin_secret",
        "mp_access_token",
        "r2_access_key_id",
        "google_client_id",
        "redis_url",
    ):
        assert not getattr(settings, name), f"{name} tiene que estar vacio en los tests"
    # Los limites por IP se prueban aparte si hace falta; en la suite todos los
    # requests vienen de la misma IP y cortarian tests que no tienen que ver.
    limiter.enabled = False
    yield


@pytest.fixture
def maintenance_url() -> str:
    return MAINTENANCE_URL


@pytest.fixture
def db_session():
    from sqlalchemy.orm import Session

    from app.database import engine

    connection = engine.connect()
    outer = connection.begin()
    session = Session(
        bind=connection, join_transaction_mode="create_savepoint", autoflush=False
    )
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture
def client(db_session):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def outbox(monkeypatch):
    """Reemplaza los `send_*` que usa el router de auth por un registro en memoria.
    Sin esto no hay forma de leer el link de verificacion del alta (con
    `RESEND_API_KEY` vacia el mail solo se loguea)."""
    from app.routers import auth

    sent: list[tuple[str, tuple]] = []
    for name in dir(auth):
        if name.startswith("send_") and callable(getattr(auth, name)):
            monkeypatch.setattr(
                auth, name, lambda *args, _n=name: sent.append((_n, args))
            )
    return sent


PASSWORD = "clave-de-prueba-123"


@pytest.fixture
def make_user(db_session):
    """Crea un usuario con los legales vigentes aceptados. `**overrides` pisa
    cualquier columna (p. ej. `privacy_version="legacy-backfill"`)."""
    from datetime import UTC, datetime

    from app.config import settings
    from app.models import User
    from app.security import hash_password

    counter = {"n": 0}

    def _make(**overrides):
        counter["n"] += 1
        now = datetime.now(UTC)
        fields = dict(
            email=f"user{counter['n']}@example.com",
            full_name="Usuario de Prueba",
            password_hash=hash_password(PASSWORD),
            terms_accepted_at=now,
            terms_version=settings.terms_version,
            privacy_accepted_at=now,
            privacy_version=settings.privacy_version,
        )
        fields.update(overrides)
        user = User(**fields)
        db_session.add(user)
        db_session.commit()
        return user

    return _make


@pytest.fixture
def login(client):
    """Inicia sesion por el endpoint real; la cookie queda en el `client`."""

    def _login(user, password: str = PASSWORD):
        res = client.post("/auth/login", json={"email": user.email, "password": password})
        assert res.status_code == 200, res.text
        assert res.json()["otp_required"] is False
        return res

    return _login

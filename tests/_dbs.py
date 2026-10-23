"""Bases efimeras para los tests y corrida de Alembic en un subproceso.

Cada corrida de pytest crea su propia base (`corpolab_run_<hex>_test`) y cada test
de migraciones otra mas (`corpolab_mig_<hex>_test`), y las borra al terminar. La
base de `TEST_DATABASE_URL` es solo de mantenimiento: ahi se emiten los CREATE y
DROP DATABASE, nunca se guardan datos ni se corre un downgrade.

Alembic corre en un SUBPROCESO a proposito: `alembic/env.py` lee la URL de
`app.config.settings`, que se carga una sola vez por proceso. En el mismo proceso
de pytest, una migracion contra la base efimera de migraciones terminaria usando
la URL que haya quedado cacheada.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests._safety import assert_ephemeral_name, assert_restricted_role, assert_safe_url

REPO_ROOT = Path(__file__).resolve().parent.parent


def _maintenance_engine(maintenance_url: str):
    assert_safe_url(maintenance_url)
    return create_engine(maintenance_url, isolation_level="AUTOCOMMIT")


def create_database(maintenance_url: str, prefix: str) -> str:
    """Crea una base vacia (desde template0) y devuelve su URL. El nombre siempre
    termina en `_test`, asi que las guardas de `_safety` la aceptan."""
    name = f"corpolab_{prefix}_{uuid.uuid4().hex[:10]}_test"
    assert_ephemeral_name(name)
    engine = _maintenance_engine(maintenance_url)
    try:
        with engine.connect() as conn:
            assert_restricted_role(conn)
            conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    finally:
        engine.dispose()
    url = make_url(maintenance_url).set(database=name).render_as_string(hide_password=False)
    assert_safe_url(url)
    return url


def drop_database(maintenance_url: str, url: str) -> None:
    """Borra una base efimera. `WITH (FORCE)` corta las conexiones que hayan
    quedado abiertas (un test que fallo a mitad de camino)."""
    assert_safe_url(url)
    name = make_url(url).database
    assert_ephemeral_name(name)
    engine = _maintenance_engine(maintenance_url)
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        engine.dispose()


def app_env(database_url: str) -> dict[str, str]:
    """Variables de entorno de la app bajo test. Pisan lo que traiga un `.env`
    local (pydantic-settings le da prioridad al entorno), asi un `.env` con
    credenciales reales no puede filtrarse a la corrida: sin claves de Resend,
    Turnstile, R2, Mercado Pago ni Google, y sin Redis."""
    return {
        "ENVIRONMENT": "test",
        "DATABASE_URL": database_url,
        "SESSION_SECRET": "test-session-secret-no-es-un-secreto",
        "REDIS_URL": "",
        "OTP_ENABLED": "false",
        "RESEND_API_KEY": "",
        "TURNSTILE_SECRET_KEY": "",
        "TURNSTILE_ENFORCE": "false",
        "ORIGIN_SECRET": "",
        "GOOGLE_CLIENT_ID": "",
        "MP_ACCESS_TOKEN": "",
        "MP_WEBHOOK_SECRET": "",
        "R2_ACCOUNT_ID": "",
        "R2_ACCESS_KEY_ID": "",
        "R2_SECRET_ACCESS_KEY": "",
        "R2_BUCKET": "",
    }


def run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    """Corre `alembic <args>` contra `database_url` en un subproceso."""
    assert_safe_url(database_url)
    env = {**os.environ, **app_env(database_url)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic {' '.join(args)} fallo ({result.returncode}):\n{result.stderr}"
        )
    return result

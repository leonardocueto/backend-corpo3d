"""Guardas que impiden que la suite toque una base que no sea de tests.

Los tests de migraciones hacen `downgrade` y los de endpoints escriben y borran
filas: apuntados por error a produccion (Neon) serian destructivos. Por eso, antes
de conectarse a nada, `conftest.py` exige las tres condiciones de abajo, y aborta
la corrida entera si alguna falla:

1. Host local (o el nombre del servicio de docker-compose / CI).
2. Nombre de base terminado en `_test`.
3. Un rol SIN privilegios de administracion (ver `tests/setup_test_db.sql`).

La 1 y la 2 se chequean sobre la URL, sin conectarse. La 3 necesita una conexion,
pero es de solo lectura (`pg_roles`). Ademas, la corrida no arranca en una maquina
configurada como produccion (`ENVIRONMENT=production`, que tienen Render y QA).

Las bases que la suite crea y borra tienen un nombre fijo
(`corpolab_run_<hex>_test` / `corpolab_mig_<hex>_test`): el DROP solo acepta ese
patron, asi que un error en el codigo de los tests no puede borrar otra base.
"""

import os
import re

from sqlalchemy import text
from sqlalchemy.engine import Connection, make_url

ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "db", "postgres"}
TEST_SUFFIX = "_test"
EPHEMERAL_DB = re.compile(r"^corpolab_(run|mig)_[0-9a-f]{10}_test$")


class UnsafeTestDatabase(RuntimeError):
    """La configuracion apunta a algo que no es una base de tests."""


def assert_not_production() -> None:
    """Se llama ANTES de que la suite pise `ENVIRONMENT`: mira el entorno real."""
    if os.environ.get("ENVIRONMENT", "").strip().lower() == "production":
        raise UnsafeTestDatabase(
            "ENVIRONMENT=production: la suite no corre en una maquina de produccion."
        )


def assert_ephemeral_name(name: str | None) -> None:
    """Solo las bases que crea la propia suite se pueden borrar."""
    if not name or not EPHEMERAL_DB.fullmatch(name):
        raise UnsafeTestDatabase(f"No es una base efimera de la suite: {name!r}.")


def assert_safe_url(url: str) -> None:
    """Valida host y nombre de base. No se conecta."""
    parsed = make_url(url)
    if parsed.host not in ALLOWED_HOSTS:
        raise UnsafeTestDatabase(
            f"Host no permitido para tests: {parsed.host!r}. "
            f"Solo {sorted(ALLOWED_HOSTS)}."
        )
    if not parsed.database or not parsed.database.endswith(TEST_SUFFIX):
        raise UnsafeTestDatabase(
            f"La base de tests tiene que terminar en {TEST_SUFFIX!r}: {parsed.database!r}."
        )


def assert_restricted_role(conn: Connection) -> None:
    """Valida que el rol conectado no tenga privilegios de administracion y que
    pueda crear bases (lo necesita para armar las efimeras de cada corrida)."""
    row = conn.execute(
        text(
            "SELECT rolsuper, rolcreaterole, rolreplication, rolbypassrls, rolcreatedb, "
            "current_database() FROM pg_roles WHERE rolname = current_user"
        )
    ).one()
    superuser, createrole, replication, bypassrls, createdb, database = row
    if superuser or createrole or replication or bypassrls:
        raise UnsafeTestDatabase(
            "El rol de tests tiene privilegios de mas (superuser / createrole / "
            "replication / bypassrls). Usar el rol de tests/setup_test_db.sql."
        )
    if not createdb:
        raise UnsafeTestDatabase("El rol de tests necesita CREATEDB (ver tests/setup_test_db.sql).")
    if not database.endswith(TEST_SUFFIX):
        raise UnsafeTestDatabase(f"Conectado a una base que no es de tests: {database!r}.")

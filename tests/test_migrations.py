"""Migraciones contra Postgres real, en bases EFIMERAS propias.

Nunca corren contra la base de la corrida ni contra la de mantenimiento: cada test
crea una base desde template0, la usa y la borra. Un downgrade es destructivo por
definicion, y aca se ejecutan todos.
"""

import pytest
from sqlalchemy import create_engine, inspect, text

from tests._dbs import create_database, drop_database, run_alembic


@pytest.fixture
def scratch_db(maintenance_url):
    url = create_database(maintenance_url, "mig")
    try:
        yield url
    finally:
        drop_database(maintenance_url, url)


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return set(inspect(conn).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_downgrade_upgrade(scratch_db):
    """Todas las migraciones suben, bajan hasta la base vacia y vuelven a subir.
    Si alguna `downgrade()` esta rota o deja restos, la segunda subida falla."""
    run_alembic(scratch_db, "upgrade", "head")
    assert {"users", "sessions", "user_tiers", "export_logs"} <= _tables(scratch_db)

    run_alembic(scratch_db, "downgrade", "base")
    assert _tables(scratch_db) <= {"alembic_version"}

    run_alembic(scratch_db, "upgrade", "head")
    assert "users" in _tables(scratch_db)


def test_una_sola_cabeza(scratch_db):
    """Dos migraciones con el mismo `down_revision` (dos ramas mergeadas sin
    reconciliar) rompen `upgrade head` en el deploy. Se detecta aca."""
    out = run_alembic(scratch_db, "heads").stdout.strip().splitlines()
    assert len(out) == 1, out


def test_los_modelos_coinciden_con_las_migraciones(scratch_db):
    """`alembic check` compara los modelos contra el schema que dejan las
    migraciones. Si alguien cambia `models.py` sin escribir la migracion, falla."""
    run_alembic(scratch_db, "upgrade", "head")
    run_alembic(scratch_db, "check")


def test_upgrade_conserva_datos_existentes(scratch_db):
    """Una base con usuarios reales (en la penultima revision) sube a head sin
    perder filas. Es el escenario de cada deploy: la migracion corre sobre datos.
    Las fases siguientes agregan aca sus asserts sobre las columnas nuevas."""
    revisions = run_alembic(scratch_db, "history").stdout.strip().splitlines()
    if len(revisions) < 2:
        pytest.skip("Hace falta al menos una revision previa a head")
    run_alembic(scratch_db, "upgrade", "head")
    run_alembic(scratch_db, "downgrade", "-1")

    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, email, is_active, is_admin, auth_provider) "
                    "VALUES (gen_random_uuid(), 'existente@example.com', true, false, 'password')"
                )
            )
        run_alembic(scratch_db, "upgrade", "head")
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT count(*) FROM users WHERE email = 'existente@example.com'")
            ).scalar_one()
        assert count == 1
    finally:
        engine.dispose()

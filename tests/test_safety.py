"""Las guardas que impiden correr la suite contra una base que no sea de tests."""

import pytest
from sqlalchemy import create_engine

from tests._safety import (
    UnsafeTestDatabase,
    assert_ephemeral_name,
    assert_not_production,
    assert_restricted_role,
    assert_safe_url,
)


@pytest.mark.parametrize(
    "url",
    [
        # Host remoto (Neon) aunque la base se llame *_test.
        "postgresql+psycopg://u:p@ep-cool-name.us-east-2.aws.neon.tech/corpolab_test",
        # Host local pero base de desarrollo / produccion.
        "postgresql+psycopg://u:p@localhost:5432/dashboard",
        "postgresql+psycopg://u:p@localhost:5432/neondb",
        # El sufijo tiene que estar al FINAL, no en cualquier parte del nombre.
        "postgresql+psycopg://u:p@localhost:5432/corpolab_test_prod",
        # Sin base.
        "postgresql+psycopg://u:p@localhost:5432",
    ],
)
def test_rechaza_urls_que_no_son_de_tests(url):
    with pytest.raises(UnsafeTestDatabase):
        assert_safe_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://u:p@localhost:5432/corpolab_test",
        "postgresql+psycopg://u:p@127.0.0.1/corpolab_run_abc_test",
        "postgresql+psycopg://u:p@db:5432/corpolab_test",
    ],
)
def test_acepta_urls_de_tests(url):
    assert_safe_url(url)


@pytest.mark.parametrize("value", ["production", "Production", " production "])
def test_no_corre_en_produccion(monkeypatch, value):
    monkeypatch.setenv("ENVIRONMENT", value)
    with pytest.raises(UnsafeTestDatabase):
        assert_not_production()


def test_corre_fuera_de_produccion(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert_not_production()
    monkeypatch.delenv("ENVIRONMENT")
    assert_not_production()


@pytest.mark.parametrize(
    "name",
    [
        None,
        "corpolab_test",  # la base de mantenimiento no se borra nunca
        "dashboard",
        "corpolab_run_ABCDEF0123_test",  # hex en mayusculas: no lo genera la suite
        "corpolab_run_abc_test",
        "corpolab_other_0123456789_test",
        "corpolab_run_0123456789_test; DROP DATABASE x",
    ],
)
def test_el_drop_solo_acepta_bases_efimeras(name):
    with pytest.raises(UnsafeTestDatabase):
        assert_ephemeral_name(name)


@pytest.mark.parametrize("name", ["corpolab_run_0123456789_test", "corpolab_mig_abcdef0123_test"])
def test_el_drop_acepta_las_bases_de_la_suite(name):
    assert_ephemeral_name(name)


def test_la_corrida_usa_un_rol_restringido(maintenance_url):
    """El rol con el que corre la suite no tiene privilegios de administracion.
    Si alguien configura TEST_DATABASE_URL con el superusuario, esto falla (y
    antes, `create_database` ya habria abortado la corrida)."""
    engine = create_engine(maintenance_url)
    try:
        with engine.connect() as conn:
            assert_restricted_role(conn)
    finally:
        engine.dispose()


def test_la_app_apunta_a_la_base_efimera():
    from app.config import settings
    from sqlalchemy.engine import make_url

    name = make_url(settings.database_url).database
    assert name.startswith("corpolab_run_") and name.endswith("_test")

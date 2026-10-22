"""El aislamiento entre tests se sostiene aunque el endpoint haga `commit()`.

Si el patron de savepoints de `conftest.py` dejara de funcionar (por un cambio de
SQLAlchemy, o porque alguien abre una sesion propia en vez de usar `get_db`), los
datos de un test se filtrarian al siguiente y los fallos aparecerian al azar.
Estos tests lo detectan de forma directa.
"""

from sqlalchemy import create_engine, text

EMAIL = "aislamiento@example.com"


def _count_from_fresh_connection(email: str) -> int:
    """Cuenta usuarios con ese email desde una conexion NUEVA, fuera de la
    transaccion del test: es lo que veria cualquier otro proceso."""
    from app.config import settings

    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT count(*) FROM users WHERE email = :e"), {"e": email}
            ).scalar_one()
    finally:
        engine.dispose()


def test_1_un_endpoint_que_commitea_no_escapa_de_la_transaccion(client, outbox):
    res = client.post(
        "/auth/signup",
        json={
            "email": EMAIL,
            "password": "clave-de-prueba-123",
            "accepted_terms": True,
            "accepted_privacy": True,
        },
    )
    assert res.status_code == 202
    link = next(args[1] for name, args in outbox if name == "send_signup_verification_email")
    token = link.split("token=")[1]
    assert client.post("/auth/verify-signup", json={"token": token}).status_code == 201

    # Dentro del test el usuario existe (la sesion del test lo ve)...
    assert client.post(
        "/auth/login", json={"email": EMAIL, "password": "clave-de-prueba-123"}
    ).status_code == 200
    # ...pero para cualquier otra conexion no: el commit del endpoint solo libero
    # un savepoint dentro de la transaccion externa.
    assert _count_from_fresh_connection(EMAIL) == 0


def test_2_el_test_anterior_no_dejo_nada():
    # Corre despues del anterior (pytest respeta el orden del archivo).
    assert _count_from_fresh_connection(EMAIL) == 0


def test_3_la_sesion_del_test_tampoco_lo_ve(db_session):
    from sqlalchemy import select

    from app.models import User

    assert db_session.scalar(select(User).where(User.email == EMAIL)) is None

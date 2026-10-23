"""Regresion de los flujos de auth que ya existen.

No prueba funcionalidad nueva: es la red para que los cambios de las fases
siguientes (perfil, consentimientos, version de la Politica) no rompan el alta,
el login, los legales ni la baja de cuenta.
"""

SIGNUP = {
    "email": "nuevo@example.com",
    "password": "clave-de-prueba-123",
    "full_name": "Nueva Cuenta",
    "accepted_terms": True,
    "accepted_privacy": True,
}


def _signup_token(client, outbox, payload=SIGNUP) -> str:
    res = client.post("/auth/signup", json=payload)
    assert res.status_code == 202, res.text
    link = next(args[1] for name, args in outbox if name == "send_signup_verification_email")
    return link.split("token=")[1]


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_alta_completa_login_me_y_logout(client, outbox):
    token = _signup_token(client, outbox)

    res = client.post("/auth/verify-signup", json={"token": token})
    assert res.status_code == 201
    assert res.json()["email"] == SIGNUP["email"]
    # Sin cookie: hay que ingresar despues de confirmar.
    assert client.get("/auth/me").status_code == 401

    res = client.post(
        "/auth/login", json={"email": SIGNUP["email"], "password": SIGNUP["password"]}
    )
    assert res.status_code == 200
    me = client.get("/auth/me").json()
    assert me["terms_accepted"] is True and me["privacy_accepted"] is True
    # UserOut no expone nada sensible.
    assert "password_hash" not in me and "created_at" not in me

    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/me").status_code == 401


def test_el_token_de_alta_es_de_un_solo_uso(client, outbox):
    token = _signup_token(client, outbox)
    assert client.post("/auth/verify-signup", json={"token": token}).status_code == 201
    assert client.post("/auth/verify-signup", json={"token": token}).status_code == 400


def test_alta_sin_aceptar_los_legales_da_422(client):
    for missing in ("accepted_terms", "accepted_privacy"):
        payload = {k: v for k, v in SIGNUP.items() if k != missing}
        assert client.post("/auth/signup", json=payload).status_code == 422


def test_alta_con_email_ya_registrado_da_409(client, make_user):
    user = make_user()
    res = client.post("/auth/signup", json={**SIGNUP, "email": user.email})
    assert res.status_code == 409


def test_login_con_clave_incorrecta_da_401(client, make_user):
    user = make_user()
    res = client.post("/auth/login", json={"email": user.email, "password": "otra-clave-999"})
    assert res.status_code == 401


def test_legales_pendientes_bloquean_los_endpoints_privados(client, make_user, login):
    user = make_user(privacy_version="legacy-backfill")
    login(user)

    me = client.get("/auth/me").json()
    assert me["terms_accepted"] is True and me["privacy_accepted"] is False

    res = client.get("/exports/attempts")
    assert res.status_code == 403
    assert res.json()["detail"] == "legal_acceptance_required"

    res = client.post("/auth/accept-legal", json={"accept_terms": False, "accept_privacy": True})
    assert res.status_code == 200
    assert res.json()["privacy_accepted"] is True
    assert client.get("/exports/attempts").status_code == 200


def test_baja_de_cuenta(client, make_user, login):
    user = make_user()
    login(user)

    assert client.post("/auth/deactivate").status_code == 204
    assert client.get("/auth/me").status_code == 401
    res = client.post(
        "/auth/login", json={"email": user.email, "password": "clave-de-prueba-123"}
    )
    assert res.status_code == 403

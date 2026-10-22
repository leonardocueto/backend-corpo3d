"""Regresion del gate de descarga (`POST /exports/download`).

R2 no se toca: la subida del .txt de auditoria se reemplaza por un registro en
memoria. El resto corre de verdad: intentos, estructura premium, agujeros, PDF.
"""

import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def uploaded_logs(monkeypatch):
    from app.routers import exports

    uploads: list[str] = []
    monkeypatch.setattr(exports, "put_export_log", lambda key, content: uploads.append(key))
    return uploads


def _download(client, structure_id="open-inner-wall", hole_count=0, extra_files=()):
    files = [("files", ("acrilico/letra.dxf", b"0\nEOF\n", "application/octet-stream"))]
    files += list(extra_files)
    return client.post(
        "/exports/download",
        files=files,
        data={"structure_id": structure_id, "project_name": "Prueba", "hole_count": str(hole_count)},
    )


def _paid(db_session, user):
    from app.models import UserTier

    db_session.add(
        UserTier(
            user_id=user.id,
            tier="mensual",
            paid_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
    )
    db_session.commit()


def test_sin_sesion_da_401(client):
    assert _download(client).status_code == 401


def test_free_descuenta_intentos_hasta_agotarlos(client, make_user, login, uploaded_logs):
    login(make_user())
    assert client.get("/exports/attempts").json()["remaining"] == 3

    for left in (2, 1, 0):
        res = _download(client)
        assert res.status_code == 200
        assert res.headers["content-type"] == "application/zip"
        assert client.get("/exports/attempts").json()["remaining"] == left

    res = _download(client)
    assert res.status_code == 403
    assert "intentos" in res.json()["detail"]
    assert len(uploaded_logs) == 3


def test_free_no_descarga_estructura_premium_ni_agujeros(client, make_user, login):
    login(make_user())
    assert _download(client, structure_id="neon-3d").json()["detail"] == "premium_structure"
    assert _download(client, hole_count=2).json()["detail"] == "premium_holes"
    # Los rechazos no consumen intentos.
    assert client.get("/exports/attempts").json()["remaining"] == 3


def test_free_no_recibe_el_pdf(client, make_user, login):
    login(make_user())
    pdf = ("files", ("ficha.pdf", b"%PDF-1.4", "application/pdf"))
    res = _download(client, extra_files=[pdf])
    names = zipfile.ZipFile(io.BytesIO(res.content)).namelist()
    assert names == ["acrilico/letra.dxf"]


def test_pago_descarga_premium_sin_limite(client, db_session, make_user, login):
    user = make_user()
    _paid(db_session, user)
    login(user)

    assert client.get("/exports/attempts").json()["unlimited"] is True
    pdf = ("files", ("ficha.pdf", b"%PDF-1.4", "application/pdf"))
    res = _download(client, structure_id="neon-3d", hole_count=2, extra_files=[pdf])
    assert res.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(res.content)).namelist()
    assert sorted(names) == ["acrilico/letra.dxf", "ficha.pdf"]

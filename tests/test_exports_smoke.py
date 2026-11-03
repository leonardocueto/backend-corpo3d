"""Regresion del gate de descarga (`POST /exports/download`).

R2 no se toca: la subida del .txt de auditoria se reemplaza por un registro en
memoria. El resto corre de verdad: intentos, estructura premium, agujeros, PDF.
"""

import io
import zipfile
from datetime import UTC, datetime, timedelta

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
            paid_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=30),
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


def _shift_window(db_session, user, days):
    """Corre el inicio de la ventana del usuario `days` dias hacia atras."""
    from sqlalchemy import select

    from app.models import ExportWindow

    win = db_session.scalar(select(ExportWindow).where(ExportWindow.user_id == user.id))
    win.window_start -= timedelta(days=days)
    db_session.commit()


def test_free_la_ventana_dura_siete_dias(client, db_session, make_user, login):
    user = make_user()
    login(user)
    for _ in range(3):
        assert _download(client).status_code == 200

    attempts = client.get("/exports/attempts").json()
    reset_at = datetime.fromisoformat(attempts["reset_at"])
    assert attempts["limit"] == 3
    assert abs(reset_at - (datetime.now(UTC) + timedelta(days=7))) < timedelta(minutes=1)

    # A los 6 dias sigue bloqueado: ya no es una ventana de 24h.
    _shift_window(db_session, user, days=6)
    assert client.get("/exports/attempts").json()["remaining"] == 0
    assert _download(client).status_code == 403

    # Pasados los 7 dias vuelve el cupo completo, y la descarga abre una ventana nueva.
    _shift_window(db_session, user, days=2)
    assert client.get("/exports/attempts").json()["remaining"] == 3
    assert _download(client).status_code == 200
    assert client.get("/exports/attempts").json()["remaining"] == 2

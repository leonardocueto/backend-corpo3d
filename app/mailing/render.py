"""Renderizado de emails transaccionales con Jinja2.

Templates branded (tema claro CorpoLab) en `templates/`: un `base.html` con la card
centrada que incluye `header.html` (logo-texto) + `footer.html` (logo-completo +
soporte); cada mail extiende `base.html` y solo aporta su bloque de contenido. Asi el
header y el footer se comparten sin repetir el wrapper.

`render_email` inyecta el contexto global (base publica del CDN para los logos + email
de soporte). Autoescape ON: las variables (link, email, codigo) se escapan solas, mas
seguro que el `str.replace` de antes.
"""

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings

_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
)


def render_email(template_name: str, **ctx: object) -> str:
    """Renderiza un template de `templates/` a HTML listo para enviar.

    Contexto global inyectado en todos los mails:
    - `assets_base_url`: base publica de los logos (`{FRONTEND_URL}/logo/...png`), el
      CDN del front (Cloudflare Pages). Los mails NO cargan imagenes del backend.
    - `support_email`: casilla de contacto que muestra el footer.
    """
    template = _env.get_template(template_name)
    return template.render(
        assets_base_url=settings.frontend_url.rstrip("/"),
        support_email="info@corpolab3d.com",
        **ctx,
    )


def format_date(value: datetime | None) -> str:
    """Formato unico de fecha para los mails ("%d/%m/%Y"); "" si no hay valor."""
    return value.strftime("%d/%m/%Y") if value else ""


def format_amount(value: int | float | None) -> str:
    """Formato unico de monto para los mails: separador de miles con punto, sin decimales."""
    return f"{(value or 0):,.0f}".replace(",", ".")

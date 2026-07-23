import hmac
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("app.origin_guard")

# Nombre del header (bytes, minuscula) que Cloudflare inyecta via Transform Rule
# en cada request que proxea hacia api.corpolab3d.com. En ASGI los nombres de
# header ya vienen en minuscula, asi que comparamos directo.
HEADER_NAME = b"x-origin-secret"

# Paths EXENTOS del guard. `/health` es critico: el health check de Render pega
# DIRECTO a *.onrender.com (sin pasar por Cloudflare, o sea sin el header). Si lo
# bloqueamos, Render marca el servicio caido y lo reinicia en loop.
EXEMPT_PATHS = frozenset({"/health"})


class OriginGuardMiddleware:
    """Rechaza con 403 todo request que NO traiga `x-origin-secret` con el valor
    esperado. Cloudflare (api.corpolab3d.com) lo inyecta; el acceso directo a
    backend-corpo3d.onrender.com no lo trae y queda bloqueado (Render mete todo
    *.onrender.com detras de SU Cloudflare, por eso NO alcanza con mirar CF-Ray:
    el discriminador real es este header).

    Middleware ASGI puro (no BaseHTTPMiddleware) para no interferir con streaming
    ni background tasks. Se registra ANTES que CORSMiddleware para que CORS quede
    outermost: asi un 403 del guard sale con headers CORS y el navegador no lo
    confunde con un error de CORS.
    """

    def __init__(self, app: ASGIApp, secret: str) -> None:
        self.app = app
        # Precomputa los bytes esperados una sola vez (no reencodear por request).
        self._expected = secret.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Solo HTTP. Lifespan/websocket pasan derecho.
        if scope["type"] != "http" or scope["path"] in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        provided = b""
        for name, value in scope["headers"]:
            if name == HEADER_NAME:
                provided = value
                break

        # compare_digest en bytes: comparacion en tiempo constante (no filtra el
        # secreto por timing). Con provided vacio da False -> 403.
        if not hmac.compare_digest(provided, self._expected):
            response = JSONResponse({"detail": "Forbidden"}, status_code=403)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

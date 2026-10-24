"""Medicion de tiempos por etapa para el header `Server-Timing`.

`RequestStartMiddleware` estampa el instante en que el request entra a la app. Hace
falta porque en un endpoint con `UploadFile` el body se recibe y el multipart se parsea
ANTES de que corra el handler (FastAPI resuelve los parametros primero): medido desde
adentro del handler, ese tramo no se ve, y en un export de decenas de MB contra el plan
Free de QA es justamente el candidato a ser el mas largo.

Es un middleware ASGI puro, no responde nada: solo deja `scope["state"]["t0"]`, que
Starlette expone como `request.state.t0`.
"""

import time


class RequestStartMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})["t0"] = time.perf_counter()
        await self.app(scope, receive, send)


def server_timing(stages: dict[str, float]) -> str:
    """`{"zip": 0.0123}` (segundos) -> `zip;dur=12.3` (ms), en el orden dado."""
    return ", ".join(f"{name};dur={seconds * 1000:.1f}" for name, seconds in stages.items())

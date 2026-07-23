"""Rate limiting compartido (slowapi).

Se keyea por el IP REAL del cliente leyendo `CF-Connecting-IP`, el header que
Cloudflare SIEMPRE sobrescribe con el IP de la conexion (no falsificable: descarta
cualquier valor que mande el cliente). Con fallback a `get_remote_address` para
dev local (sin Cloudflare adelante).

Por que NO `get_remote_address` a secas: detras de Cloudflare, uvicorn
(`--forwarded-allow-ips="*"`, ver start.sh) toma el PRIMER valor de
`X-Forwarded-For`. Cloudflare APPENDEA a ese header, no lo reescribe, asi que un
atacante que manda su propio `X-Forwarded-For` controla ese primer valor y lo
puede rotar para saltear el limite (p. ej. el 5/min de /auth/login). Medido en
prod (2026-07-23): con XFF el valor es spoofable; `CF-Connecting-IP` no.

Por que `CF-Connecting-IP` es confiable como key aca: (1) el guard de origen
(`origin_guard.py`) ya garantiza que TODO el trafico paso por tu Cloudflare, y
(2) se verifico empiricamente que el header sobrevive intacto los DOS Cloudflares
de la cadena (el tuyo + el que Render pone delante de *.onrender.com): el segundo
NO lo pisa.

Almacenamiento en memoria (default): suficiente para una sola instancia (Render
free). Para multiples instancias o persistencia entre reinicios haria falta un
backend Redis (`storage_uri`).
"""

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request


def client_ip(request: Request) -> str:
    """IP real del visitante para keyear el rate limit: `CF-Connecting-IP` (no
    spoofable detras de Cloudflare) con fallback a la IP remota en dev local."""
    return request.headers.get("cf-connecting-ip") or get_remote_address(request)


limiter = Limiter(key_func=client_ip)

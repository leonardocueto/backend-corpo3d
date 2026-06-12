"""Rate limiting compartido (slowapi).

`key_func = get_remote_address` limita por IP del cliente. En prod detras del
proxy de Render, esa IP es la real solo si uvicorn corre con
`--proxy-headers --forwarded-allow-ips="*"` (ver start.sh); si no, todas las
peticiones comparten la IP del proxy y se limitarian juntas.

Almacenamiento en memoria (default): suficiente para una sola instancia (Render
free). Para multiples instancias o persistencia entre reinicios haria falta un
backend Redis (`storage_uri`).
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

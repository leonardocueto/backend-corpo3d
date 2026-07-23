from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.ratelimit import limiter
from app.routers import auth, designs, exports, payments, tiers, users

app = FastAPI(title="Dashboard API")

# Rate limiting (slowapi): registra el limiter y el handler de 429. Los limites
# por endpoint se declaran con @limiter.limit(...) en los routers.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS para cookies: NO se puede usar "*" junto con allow_credentials=True.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,   # orígenes explícitos del front
    allow_origin_regex=settings.effective_cors_origin_regex,  # dev: cualquier puerto localhost
    allow_credentials=True,                # permite enviar/recibir la cookie
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(exports.router)
app.include_router(tiers.router)
app.include_router(designs.router)
app.include_router(payments.router)


@app.get("/health")
def health():
    return {"status": "ok"}


# === TEMPORAL / DESECHABLE — verificacion "puerta Cloudflare" (BORRAR tras confirmar) ===
# Cloudflare inyecta el header `x-origin-secret` en cada request que proxea. Este
# endpoint NO compara nada: solo espeja lo que llega, para confirmar que el header
# entra por una puerta y no por la otra. Pegarle a las dos:
#   curl https://api.corpolab3d.com/__debug/origin          -> header_present: true
#   curl https://backend-corpo3d.onrender.com/__debug/origin -> header_present: false
# `via_cloudflare` es un doble-check: CF-Ray tambien lo agrega solo Cloudflare.
@app.get("/__debug/origin")
def _debug_origin(request: Request):
    raw = request.headers.get("x-origin-secret")
    preview = f"{raw[:4]}...{raw[-4:]}" if raw and len(raw) >= 8 else raw
    return {
        "header_present": raw is not None,
        "value_preview": preview,
        "value_len": len(raw) if raw else 0,
        "via_cloudflare": "cf-ray" in request.headers,
        "cf_ray": request.headers.get("cf-ray"),
    }

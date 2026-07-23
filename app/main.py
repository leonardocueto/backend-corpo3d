import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.origin_guard import OriginGuardMiddleware
from app.ratelimit import limiter
from app.routers import auth, designs, exports, payments, tiers, users

logger = logging.getLogger("app.main")

app = FastAPI(title="Dashboard API")

# Rate limiting (slowapi): registra el limiter y el handler de 429. Los limites
# por endpoint se declaran con @limiter.limit(...) en los routers.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Guard de origen: solo se activa si ORIGIN_SECRET esta seteada (FAIL-OPEN
# deliberado para dev/local y para el rollout: se deploya inactivo y se prende
# cargando la var en Render). OJO al orden: add_middleware ANTEPONE, o sea el
# ULTIMO agregado queda outermost. Registramos el guard ANTES que CORS para que
# CORS quede outermost y envuelva al guard -> un 403 sale con headers CORS.
if settings.origin_secret:
    app.add_middleware(OriginGuardMiddleware, secret=settings.origin_secret)
else:
    logger.warning("ORIGIN_SECRET ausente: guard de origen DESACTIVADO")

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

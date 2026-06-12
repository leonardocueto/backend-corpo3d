from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import auth

app = FastAPI(title="Dashboard API")

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


@app.get("/health")
def health():
    return {"status": "ok"}

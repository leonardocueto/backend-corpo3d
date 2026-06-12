#!/bin/sh
# Arranque de PRODUCCION (Render): aplica migraciones y levanta la API en el
# puerto que inyecta la plataforma ($PORT, fallback 8000). En local se usa
# docker-compose, que define su propio command, asi que este script es solo prod.
set -e

alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"

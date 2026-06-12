FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# libpq = cliente Postgres en runtime (lo usa psycopg puro-Python).
RUN apk add --no-cache libpq

COPY requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

COPY . .

EXPOSE 8000

# Prod (Render): start.sh corre migraciones + uvicorn en $PORT.
# En local, docker-compose pisa este CMD con su propio command.
CMD ["sh", "start.sh"]

# CLAUDE.md — Backend

Guía del backend para Claude Code (y humanos). Resume **qué es, cómo está armado y las
convenciones**, para no re-derivarlas en cada sesión.

## Qué es

API de **autenticación** en **FastAPI** para el dashboard interno (SPA en `../3D`). Usa
**sesión simple con cookie `HttpOnly`** persistida en Postgres: **sin JWT, sin tokens en el
front, sin localStorage**. Pensada para pocos usuarios internos que casi no consumen el
backend tras el login.

> Decisión clave: se eligió sesión-por-cookie en vez de JWT por revocación inmediata (logout
> real del lado servidor) y por no exponer el token al JS. JWT solo conviene a escala /
> multi-servicio. Ver el porqué en `PROYECTO-BACKEND.md`.

## Stack

- **FastAPI** + **Uvicorn** (ASGI)
- **SQLAlchemy 2.0** (sync, typed `Mapped[...]`) + **Alembic** (migraciones) + **PostgreSQL** (`psycopg`)
- **passlib[bcrypt]** (passwords) + **HMAC-SHA256** (hash del token de sesión, `hmac`/`hashlib` stdlib)
- **pydantic-settings** (config por `.env`)
- **Docker Compose** (Postgres alpine + API alpine)

## Comandos

```powershell
# levantar todo (build + Postgres + API; corre 'alembic upgrade head' al arrancar)
docker compose up --build -d
# crear un usuario admin (interactivo: email + password + nombre)
docker compose exec api python -m scripts.create_admin
# logs / parar / parar+borrar datos
docker compose logs -f api
docker compose down          # conserva datos (volumen pgdata)
docker compose down -v       # borra también los datos

# inspeccionar la DB
docker compose exec db psql -U postgres -d dashboard -c "SELECT email, is_admin FROM users;"
```

API en `http://localhost:8000` · Swagger en `/docs` · ReDoc en `/redoc`.

> Tras reconstruir la imagen, darle ~2 s a Uvicorn antes de pegarle (el contenedor corre sin
> `--reload`; los cambios de código requieren `docker compose up --build`).

## Estructura

```
backend/
├── app/
│   ├── main.py            # FastAPI + guard de origen + CORSMiddleware + include router + /health
│   ├── origin_guard.py    # middleware ASGI: exige header x-origin-secret (Cloudflare) o 403
│   ├── config.py          # Settings (pydantic-settings); cookie_secure es property
│   ├── database.py        # engine sync, SessionLocal, Base (DeclarativeBase), get_db
│   ├── models.py          # User, Session (typed; UUID; created_at/revoked_at)
│   ├── schemas.py         # LoginIn, RegisterIn, UserOut (salida = {id,email,full_name,is_admin})
│   ├── security.py        # hash_password/verify (bcrypt) · generate/hash_session_token (HMAC)
│   ├── deps.py            # get_current_user (valida la sesión) · require_admin
│   └── routers/auth.py    # /auth/login /me /logout /register + _set_session_cookie
├── scripts/create_admin.py# bootstrap del primer admin (CLI, valida formato de email)
├── alembic/               # env.py + versions/0001_initial.py (schema users+sessions)
├── docker-compose.yml · Dockerfile · .dockerignore
├── requirements.txt          # dev local (psycopg[binary])
├── requirements-docker.txt   # contenedor alpine (psycopg[binary] musl, uvicorn sin extras)
├── .env / .env.example · alembic.ini · README.md · PROYECTO-BACKEND.md
```

## Modelo de datos (`models.py`)

- **User**: `id` (UUID), `email` (único), `full_name`, `password_hash` (**nullable**: los
  usuarios creados por Google no tienen password), `is_active`, `is_admin`, `google_sub`
  (**único, nullable**: `sub` estable de la cuenta de Google para linkeo), `auth_provider`
  (`'password' | 'google'`, default `'password'`), `created_at`. (Columnas de Google: migración
  `0007_google_oauth`; **latentes**, hoy todos los users son `auth_provider='password'` — el login
  con Google no esta activo, ver flujo de auth #6.)
- **Session**: `id` (UUID), `user_id` (FK, `ON DELETE CASCADE`), `token_hash` (único),
  `expires_at`, `created_at`, `revoked_at` (nullable). Borrar un User borra sus sesiones.

## Flujo de auth

1. `POST /auth/login`: valida email+password (bcrypt, se verifica siempre aunque el user no
   exista, para no filtrar por timing) → genera token plano (`secrets.token_urlsafe(32)`) →
   guarda **solo** `HMAC-SHA256(token, SESSION_SECRET)` con `expires_at = now + 7 días` →
   setea cookie `HttpOnly; Max-Age=604800; Path=/; SameSite=lax` (+ `Secure` en prod).
2. `get_current_user` (dependency, `deps.py`): lee la cookie → re-hashea → busca la sesión →
   valida **existe + `revoked_at IS NULL` + no vencida + usuario activo**; si falla → `401`.
   `/auth/me` la reutiliza; **cualquier endpoint futuro protegido debe usar
   `Depends(get_current_user)`**.
3. `POST /auth/logout`: setea `revoked_at` (revocación real en DB) + borra la cookie.
4. `POST /auth/register`: `Depends(require_admin)` → solo un admin autenticado crea usuarios.
   El primer admin se crea con `scripts/create_admin.py` (huevo-gallina). El alta admin con
   tier vive además en `POST /users` (router admin-only), que es el que usa el panel.
5. `POST /auth/signup`: alta **self-serve PÚBLICA** (sin admin). Crea siempre usuario común
   (`is_admin=False`) en tier free e inicia sesión al toque (misma cookie que `login`). No
   acepta `is_admin`/`tier` del cliente (anti-escalada). Rate limit 5/min.
6. `POST /auth/google`: login con **Google (OIDC)** — **NO IMPLEMENTADO / LATENTE**. El
   codigo del backend ya existe (`app/google_oauth.py`, endpoint, migracion `0007`, columnas
   `google_sub`/`auth_provider`) pero **no esta activo end-to-end**: el front no tiene boton de
   Google (las pantallas de auth deliberadamente no ofrecen SSO) y sin `GOOGLE_CLIENT_ID` cargado
   el endpoint responde 401. Queda como base para el dia que se active. Comportamiento previsto
   cuando se implemente: el front manda el ID token (`credential`); el backend lo verifica (firma +
   `aud` == `GOOGLE_CLIENT_ID` + `exp` + `email_verified`), busca por `google_sub`, si no por email
   (linkea cuentas password del mismo email), si no **autocrea** (tier free, `password_hash=None`,
   `auth_provider='google'`), y termina con la **misma cookie** que `login`. La fuente de verdad
   sigue siendo `users` en Postgres.

## Seguridad — invariantes a NO romper

- El token plano vive **solo** en la cookie `HttpOnly`. En DB nunca el token plano, solo su
  HMAC (`String(64)` hex). El pepper es `SESSION_SECRET` (obligatorio, fuera del repo).
- `UserOut` define la salida: **no agregar campos sensibles** (ni `password_hash` ni
  `created_at`). FastAPI serializa solo lo declarado en el `response_model`.
- CORS: `allow_credentials=True` + `allow_origins` con dominios **exactos** (nunca `*`; el
  navegador lo rechaza con credenciales). Configurable por `CORS_ORIGINS` en `.env`.
- `Secure` se activa solo en prod (`ENVIRONMENT=production`) o si `SameSite=none`.
- **Guard de origen** (`origin_guard.py`): en prod el backend solo contesta a requests
  que traen el header `x-origin-secret` que inyecta Cloudflare (Transform Rule en
  `api.corpolab3d.com`); si no coincide con `ORIGIN_SECRET` → **403**. Cierra el acceso
  directo a `*.onrender.com`. **Fail-open**: sin `ORIGIN_SECRET` seteada el middleware ni
  se registra (dev/local anda sin el header) y se loguea un warning al arranque. Se registra
  **antes** que `CORSMiddleware` a propósito → CORS queda outermost y un 403 sale con headers
  CORS. **`/health` está exento** (el health check de Render pega directo, sin Cloudflare).
  NO validar `CF-Ray`: Render mete todo `*.onrender.com` detrás de su propio Cloudflare, así
  que ese header aparece por las dos puertas; el único discriminador es `x-origin-secret`.

## Despliegue / SameSite

`SameSite=Lax` solo manda la cookie en peticiones **same-site** (mismo dominio registrable).
- Dev (`localhost:3000` ↔ `localhost:8000`): same-site → `Lax`, sin `Secure`.
- Prod mismo dominio (`app.x.com` + `api.x.com`): `Lax` + `Secure`.
- Prod cross-site (`*.pages.dev` + `*.railway.app`): `COOKIE_SAMESITE=none` (fuerza `Secure`)
  + `CORS_ORIGINS` con el origen exacto, todo HTTPS.

El front debe llamar a la API con `credentials: "include"` para enviar/recibir la cookie.

## Convenciones / cuidados

- **Respuestas al usuario (chat)**: español SIN tildes/acentos (ej. "Confirmas"). Solo el chat,
  no el código ni estos docs.
- Lógica de validación de sesión: **una sola** (`get_current_user`); no duplicarla por endpoint.
- Cambios de schema → nueva migración Alembic en `alembic/versions/`, no editar la `0001`.
- `psycopg[binary]` en local (Windows/glibc) y en el contenedor (hay wheels musllinux; trae
  `libpq` embebida — psycopg puro falla en Alpine por `ctypes.find_library`).

## TODO / pendiente

- Conectar el frontend Nuxt (página `/login`, middleware de auth, composable `useAuth`,
  capa de servicio con `credentials: "include"` y el fetching nativo de Nuxt 4).
- Limpieza de sesiones vencidas (job periódico o `DELETE` en login).
- Rate limiting (hecho): `slowapi` por IP en `/auth/login` (5/min) y `/auth/register`
  (10/min); limiter en `app/ratelimit.py`. Requiere uvicorn con `--proxy-headers`
  (ya en `start.sh`) para tomar la IP real detrás del proxy. En memoria (1 instancia);
  para multi-instancia haría falta Redis.
- **Login con Google (OIDC): NO implementado / LATENTE.** El código del backend ya existe
  (`app/google_oauth.py`, endpoint `/auth/google`, migración `0007`, columnas
  `google_sub`/`auth_provider`) pero no está activo end-to-end: el front no ofrece SSO y sin
  `GOOGLE_CLIENT_ID` el endpoint responde 401. Queda como base para el día que se active
  (ver flujo de auth #6). Todos los users hoy son `auth_provider='password'`.
- **Guard de origen: activación en Render pendiente.** El código está deployado inactivo
  (fail-open sin `ORIGIN_SECRET`). Para prenderlo: cargar `ORIGIN_SECRET` en el dashboard de
  Render (mismo valor de 64 hex que la Transform Rule de Cloudflare) → reinicia y se activa.
  Verificar luego: login desde www ok · `/health` directo = 200 · `/auth/me` directo = 403.

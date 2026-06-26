# Dashboard API — Backend (FastAPI + sesión por cookie)

Backend de autenticación para el SPA interno. Usa **sesión simple con cookie `HttpOnly`**
persistida en Postgres (sin JWT, sin tokens en el front). La cookie dura **7 días**.

## Stack
- FastAPI + Uvicorn
- SQLAlchemy 2.0 (sync) + Alembic + PostgreSQL (`psycopg`)
- `passlib[bcrypt]` (passwords) + HMAC-SHA256 (hash del token de sesión)

## Endpoints
| Método | Ruta | Auth | Descripción |
| --- | --- | --- | --- |
| POST | `/auth/login` | — | Valida email+password, crea sesión y setea la cookie. |
| POST | `/auth/signup` | — | **Registro público**: crea usuario común (tier free, no admin) e inicia sesión. |
| POST | `/auth/google` | — | **Login con Google** (OIDC, latente: 401 sin `GOOGLE_CLIENT_ID`). Autocrea/linkea y abre sesión. |
| GET | `/auth/me` | cookie | Devuelve el usuario de la sesión actual (401 si no es válida). |
| POST | `/auth/logout` | cookie | Revoca la sesión y borra la cookie. |
| POST | `/auth/register` | admin | Crea un usuario (solo un admin autenticado). El alta con tier vive en `POST /users`. |
| POST | `/auth/change-password` | cookie | Cambia la propia contraseña (verifica la actual). |
| POST | `/auth/forgot-password` | — | Pide link de reset por email (responde 204 siempre, anti-enumeración). |
| POST | `/auth/reset-password` | — | Setea nueva contraseña con el token del email (single-use). |
| GET | `/health` | — | Healthcheck. |

## Setup con Docker (recomendado)

Levanta Postgres (alpine) + la API (alpine) con un comando. Requiere Docker Desktop corriendo.

```powershell
cd C:\Users\Leo\Desktop\project\backend
Copy-Item .env.example .env          # si no existe; editar SESSION_SECRET (ver abajo)
docker compose up --build -d         # build + Postgres + API; la API corre 'alembic upgrade head' al arrancar
docker compose exec api python -m scripts.create_admin   # crear el admin (interactivo)
```

Generar un `SESSION_SECRET` para el `.env`:
```powershell
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

- API: http://localhost:8000 (docs en `/docs`). Postgres expuesto en `localhost:5432`.
- `docker compose` pisa `DATABASE_URL` para apuntar al servicio `db` (no a `localhost`);
  el resto de las variables salen del `.env`.
- Ver logs: `docker compose logs -f api`. Parar: `docker compose down`
  (agregá `-v` para borrar también el volumen de datos `pgdata`).

> El contenedor usa `requirements-docker.txt` (psycopg puro + libpq, uvicorn sin extras)
> para evitar los problemas de wheels en Alpine/musl. El `requirements.txt` es para dev local.

## Setup local sin Docker (Windows / PowerShell)

```powershell
cd backend
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env
# Editar .env: poner un SESSION_SECRET aleatorio y la DATABASE_URL real.
# Generar un secreto: python -c "import secrets;print(secrets.token_urlsafe(48))"

# Crear la base 'dashboard' en Postgres si no existe, luego:
alembic upgrade head
python -m scripts.create_admin     # crea el admin inicial (email + password)

uvicorn app.main:app --reload      # http://localhost:8000  (docs en /docs)
```

> Linux/Mac: activar el venv con `source .venv/bin/activate` y usar `cp` en vez de `Copy-Item`.

## Variables de entorno (`.env`)
| Var | Default | Notas |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | `production` activa `Secure` en la cookie. |
| `DATABASE_URL` | local | `postgresql+psycopg://user:pass@host:5432/db` |
| `SESSION_SECRET` | — (obligatorio) | Pepper del HMAC del token. Largo y aleatorio. |
| `COOKIE_SAMESITE` | `lax` | `lax` \| `strict` \| `none` (ver más abajo). |
| `COOKIE_DOMAIN` | vacío | Dominio de la cookie (vacío = host actual). |
| `SESSION_DAYS` | `7` | Duración de la sesión. |
| `CORS_ORIGINS` | `["http://localhost:3000"]` | JSON array con los orígenes del front. |

## SameSite / despliegue (importante)
`SameSite=Lax` solo envía la cookie en peticiones **same-site** (mismo dominio registrable):
- **Dev** (`localhost:3000` → `localhost:8000`): same-site → `Lax` OK, sin `Secure` (http).
- **Prod mismo dominio** (`app.x.com` + `api.x.com`): `Lax` + `Secure` OK.
- **Prod cross-site** (ej. `*.pages.dev` + `*.railway.app`): usar `COOKIE_SAMESITE=none`
  (fuerza `Secure`) y `CORS_ORIGINS` con el origen exacto del front, todo por HTTPS.

El front debe llamar a la API con `credentials: "include"` para enviar/recibir la cookie.

## Pruebas rápidas (cookie jar con curl)
```powershell
# login -> 200 + Set-Cookie: session_token=...; HttpOnly
curl -i -c cookies.txt -X POST http://localhost:8000/auth/login `
  -H "Content-Type: application/json" -d '{"email":"admin@x.com","password":"TU_PASS"}'

curl -i -b cookies.txt http://localhost:8000/auth/me          # 200 con cookie
curl -i http://localhost:8000/auth/me                          # 401 sin cookie

curl -i -b cookies.txt -X POST http://localhost:8000/auth/register `
  -H "Content-Type: application/json" -d '{"email":"user@x.com","password":"otro123"}'  # 201

curl -i -b cookies.txt -X POST http://localhost:8000/auth/logout   # 204
curl -i -b cookies.txt http://localhost:8000/auth/me               # 401 (sesión revocada)
```

## Notas de seguridad
- El token plano vive solo en la cookie `HttpOnly`; en DB se guarda `HMAC-SHA256(token, SESSION_SECRET)`.
- No usar `localStorage` para el token (lo maneja el navegador con la cookie).
- `CORS` no puede usar `*` junto con `allow_credentials=True`: listar orígenes explícitos.

## Pendiente / futuro
- Limpieza de sesiones vencidas (job periódico o `DELETE` en login).
- ~~Rate limiting~~ (hecho): `slowapi` por IP en `/auth/login`, `/auth/signup`, `/auth/register`, etc.
- ~~Cablear el frontend Nuxt~~ (hecho): login, registro (`/registrarse`), recuperación de contraseña,
  middleware de auth y capa de servicio con `credentials: "include"`.
- **Login con Google** (OIDC): código listo y latente; falta cargar `GOOGLE_CLIENT_ID` para activarlo (ver `OAuth.md`).

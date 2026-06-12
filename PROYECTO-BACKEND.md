# Backend para CorpoLab 3D — guía de arquitectura y despliegue

Notas sobre cómo agregar un backend (Python + PostgreSQL + API REST con login)
al frontend actual, que hoy es 100% client-side (Nuxt `ssr: false`, SPA estática,
sin backend).

## Las 3 piezas (se despliegan distinto)

| Pieza | Qué es | Cómo se sirve |
| --- | --- | --- |
| **Frontend** (lo que ya existe) | SPA estática (`npm run generate` → `.output/public`) | Archivos estáticos en un CDN/host estático |
| **Backend API** (lo nuevo) | Proceso Python corriendo siempre, escuchando HTTP | Un servidor/contenedor que no se apaga |
| **Base de datos** | PostgreSQL | Servicio gestionado o contenedor |

Diferencia clave: el front son **archivos** (los subís y listo), el backend es un
**proceso vivo** (tiene que estar prendido escuchando peticiones).

## Stack backend recomendado (Python)

- **FastAPI** — estándar hoy para APIs REST en Python: async, validación con
  Pydantic, genera la doc OpenAPI sola (`/docs`). Más rápido de armar que
  Flask/Django para una API pura.
- **SQLAlchemy 2.0** (ORM) + **Alembic** (migraciones de schema).
- **PostgreSQL** como base de datos.
- **Auth con sesión por cookie `HttpOnly`** (NO JWT, NO localStorage): login
  (`POST /auth/login` con email+password) → validar el hash del password →
  crear un `session_token` aleatorio (`secrets.token_urlsafe`) → guardar en
  Postgres **solo el hash** del token (HMAC-SHA256) con `expires_at` (7 días) y
  `revoked_at` → enviar el token en una cookie `HttpOnly`/`Secure`/`SameSite`.
  El navegador la manda sola en cada request (con `credentials: "include"`); el
  front nunca toca ni guarda el token.
  - Passwords: `passlib[bcrypt]` o `argon2`.
  - Token de sesión: `secrets` + HMAC-SHA256 (módulo `hmac`/`hashlib` stdlib).
  > Implementado en `backend/app/` (ver `backend/README.md`). Endpoints:
  > `/auth/login`, `/auth/me`, `/auth/logout`, `/auth/register` (admin).
  >
  > **Por qué cookie de sesión y no JWT**: es un dashboard interno con pocos
  > usuarios que casi no consume el backend tras el login. La sesión en DB se
  > revoca al instante (logout/baja), no expone el token al JS (a prueba de XSS
  > robando tokens) y evita el `Authorization: Bearer` + token en `localStorage`,
  > que es justamente lo que NO queremos. JWT solo conviene a escala / multi-servicio.
- Libs típicas: `fastapi`, `uvicorn` (servidor ASGI), `sqlalchemy`, `alembic`,
  `psycopg[binary]`, `passlib[bcrypt]`, `pydantic-settings` (sin `pyjwt`).

## Cómo se despliega cada cosa

### Opción A — PaaS (lo más simple para empezar) ← recomendada
Plataformas tipo **Railway**, **Render** o **Fly.io**:
- Conectás tu repo de GitHub.
- Creás un servicio Postgres (te dan la `DATABASE_URL`).
- Creás un servicio web que corre FastAPI (`uvicorn main:app`). Detectan Python
  solo o con un `Dockerfile`.
- El frontend estático va en el mismo Render/Railway como "static site", o en
  **Vercel/Netlify/Cloudflare Pages** (gratis y rápido para estáticos).

Evita administrar servidores. Es lo recomendado para arrancar.

### Opción B — VPS propio
Un VPS (Hetzner, DigitalOcean, etc.) con Docker:
- `docker compose` con 3 servicios: API, Postgres y **Nginx** como reverse proxy
  (sirve el estático del front + enruta `/api` al backend).
- Más control y más barato a escala, pero administrás vos: HTTPS (Caddy/Traefik
  lo hacen automático), backups de la DB, actualizaciones.

### Opción C — Cloud grande (AWS/GCP/Azure)
Contenedor en ECS/Cloud Run + RDS/Cloud SQL. Más robusto y escalable, pero más
complejidad. Para cuando el proyecto lo justifique.

## Lo que cambia en el frontend

Hoy es estático sin variables de entorno de red. Va a necesitar:
- Una variable tipo `NUXT_PUBLIC_API_BASE` con la URL del backend
  (ej. `https://api.tudominio.com`).
- Manejar **CORS** en FastAPI (`CORSMiddleware`) con orígenes explícitos y
  `allow_credentials=True` (no se puede usar `*` con credenciales).
- Todas las requests al backend con `credentials: "include"` para que el navegador
  envíe la cookie de sesión.
- Flujo de login en el front: pantalla de login → `POST /auth/login` (el backend
  setea la cookie sola) → middleware de Nuxt que llama a `/auth/me` y redirige al
  login si devuelve 401. El front NO guarda ni manda el token a mano (lo hace el
  navegador vía la cookie `HttpOnly`). **No usar `localStorage`.**

## Recomendación concreta para arrancar

1. **Backend**: FastAPI + SQLAlchemy + Alembic, auth por **sesión con cookie
   `HttpOnly`** (bcrypt para passwords). Ya implementado en `backend/`.
2. **Deploy**: Railway o Render (API + Postgres juntos, mismo dashboard,
   gratis/barato para empezar).
3. **Frontend**: queda estático en Cloudflare Pages/Vercel, apuntando a la URL del
   backend.
4. **Estructura**: backend en un repo o carpeta aparte (ej. `api/`), no mezclado
   con el código Nuxt, porque son runtimes distintos.

## Próximos pasos posibles

- ~~Crear la estructura del backend FastAPI~~ → **hecho** en `backend/` (modelos
  `User`/`Session`, endpoints `/auth/login` `/auth/me` `/auth/logout` `/auth/register`,
  sesión por cookie, migración inicial de Postgres). Ver `backend/README.md`.
- Armar el `docker-compose.yml` (API + Postgres + Nginx) para la opción VPS.
- Integrar el flujo de login en el front Nuxt (pantalla `/login` + middleware de
  auth + composable `useAuth` + capa de servicio con `credentials: "include"`).

> Nota de despliegue: las cuentas (Railway/Render/etc.) y algunos comandos de
> deploy hay que correrlos manualmente; el código y la config se pueden dejar listos.

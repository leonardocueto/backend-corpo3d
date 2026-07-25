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
│   ├── ratelimit.py       # slowapi Limiter (key_func = CF-Connecting-IP, no spoofable)
│   ├── email.py           # envio Resend (send_* transaccionales) — capa aislada del proveedor
│   ├── mailing/           # templates Jinja2 branded (base+header+footer; render_email)
│   └── routers/           # auth.py + users/tiers/designs/exports/payments (MercadoPago)
├── scripts/create_admin.py    # bootstrap del primer admin (CLI, valida formato de email)
├── scripts/notify_expiring.py # job diario: aviso "vence pronto" de tiers pagos (Render Cron)
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
5. `POST /auth/signup`: alta **self-serve PÚBLICA** (sin admin) con **verificación de email
   (double opt-in)**. **NO crea el usuario ni inicia sesión**: guarda un `PendingRegistration`
   (email + `full_name` + password ya hasheado + token) y manda por email un link
   `{FRONTEND_URL}/confirmar-registro?token=...` (envío en BackgroundTask). Responde **202**
   (sin body, sin cookie). Casos: si el email ya tiene cuenta **confirmada** → **409 "Email ya
   registrado"**; si hay un pendiente sin confirmar → se **invalida y se emite uno nuevo** (un
   solo link vivo a la vez, patrón de `forgot-password`). No acepta `is_admin`/`tier`
   (anti-escalada). Rate limit 5/min. Vida del token: `SIGNUP_TOKEN_MINUTES` (default 60).
   - **`PendingRegistration`** (`models.py`, migración `0009`): espejo de `PasswordResetToken`
     pero **sin FK a `users`** (el usuario aún no existe). En DB solo el HMAC del token; `email`
     indexado NO único (se reusa tras confirmar/expirar). Se eligió tabla aparte (no `User`
     inactivo) para no ensuciar `users` ni bloquear el `unique(email)` con altas sin confirmar.
5b. `POST /auth/verify-signup`: 2do paso del double opt-in. Consume el token (single-use +
   corto; 400 genérico si no existe/usado/vencido) y **recién ahí crea el `User` real**
   (`is_admin=False`, tier free lazy, `password_hash` copiado del pending **sin re-hashear**).
   **NO inicia sesión** (no cookie): el usuario debe **ingresar de nuevo** (igual que
   `reset-password`). Idempotente: si la cuenta ya existe (doble click / carrera), marca el
   pending usado y devuelve el user existente. Email en `app/email.py`
   (`send_signup_verification_email`; sin `RESEND_API_KEY` loguea el link en dev).
   - **Mail de bienvenida (2026-07-25)**: al crear el `User` real (solo en el alta nueva, NO en
     el caso idempotente) se encola `send_welcome_email` en BackgroundTask, después del commit.
     Solo aplica al signup público; las altas de admin (`/auth/register`, `POST /users`) no
     mandan bienvenida.
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
7. **OTP de login (2do factor por email)** — código listo, se prende con `OTP_ENABLED=true`
   (default `false`; sin bloqueo técnico desde que el dominio quedó verificado en Resend). Con
   OTP ON el login es de **2 pasos**: `POST /auth/login` valida credenciales y, en vez de setear
   cookie, emite un código de 6 dígitos por email y responde `otp_required=true`;
   `POST /auth/verify-otp` consume el código y **recién ahí** inicia sesión (misma cookie). Con
   OTP OFF el login es de 1 paso como siempre. Modelo `LoginOtp` (migración `0008`): HMAC del
   código, single-use (`used_at`), corto (`OTP_MINUTES`, default **3**), tope `OTP_MAX_ATTEMPTS`
   (default 5).
   - **`POST /auth/resend-otp`** (botón "reenviar"): responde **SIEMPRE 204** (anti-enumeración).
     **Cooldown server-side**: NO emite un código nuevo mientras el usuario tenga uno **activo**
     (sin usar y sin vencer) — es la validación real del timer del front (que solo habilita
     "reenviar" al vencer el código), no salteable manipulando el cliente. Al vencer el código
     actual, el reenvío vuelve a estar disponible. (Además hay rate-limit slowapi 1/min por IP,
     que el cooldown de 3 min ya subsume.)

## Pagos (MercadoPago — Checkout Pro)

`app/routers/payments.py`. Integración **Checkout Pro** (NO Checkout API): `sdk.preference()
.create(...)` → devuelve `init_point`, el front redirige a la página de MP.

- **Anti-tamper**: el cliente manda solo `{ plan }`; el **monto lo fija el servidor** (precios
  en `config`). `GET /plans` expone los precios (la UI los lee de ahí, nada hardcodeado).
- **El tier se activa SOLO desde el webhook con firma validada** (`POST /payments/webhook`),
  nunca desde el redirect del navegador (`back_urls` es spoofeable). Idempotente por
  `Payment.mp_payment_id` UNIQUE (MP reintenta el webhook).
- **Firma del webhook** (`_valid_signature`): HMAC-SHA256 con `MP_WEBHOOK_SECRET`. Manifest
  `id:<data.id>;request-id:<x-request-id>;ts:<ts>;`. Sin secret o firma que no matchea → **401**.
- **`notification_url`** = `{BACKEND_URL}/payments/webhook` = `api.corpolab3d.com/...` → pasa
  por Cloudflare (trae `x-origin-secret`, pasa el guard) y la **Custom rule 1 (Skip)** de
  Cloudflare lo exime de todo el WAF.
- **Mails de resultado (2026-07-25)**: el webhook maneja estados **terminales** y avisa por
  email (en BackgroundTask): `approved` → activa tier + registra `Payment` + `send_payment_
  approved_email`; `rejected`/`cancelled` → registra `Payment` con ese status (auditoría +
  idempotencia) + `send_payment_rejected_email` (link "Reintentar" → `/pricing`). `pending`/
  `in_process` → sin acción (como antes). Un solo mail por pago gracias al `mp_payment_id`
  UNIQUE (los reintentos del webhook no re-mailean). El **tier NO se toca** en el fallo.

**Estado (2026-07-24): PROBADO, sin activar producción.** La firma funciona — la **"Simular
notificación"** del panel de MP da **200**. Pero los pagos de PRUEBA daban **401**: es un
**artefacto del sandbox de MP**, no un bug. Motivo: los pagos con "Credenciales de prueba" los
cobra una **cuenta test auto-generada** (≈`3483540259`) que MP firma con **otro** secreto,
distinto al del webhook de tu cuenta real (≈`257561078`, el `0f2fbd41…`). Hay **un solo
secreto por app** (igual en Modo prueba y productivo). **En producción validará** (el cobrador
será tu cuenta real, dueña del secreto). Se decidió **Camino B**: confiar en lo probado y
verificar al activar producción. `MP_ACCESS_TOKEN` y `MP_WEBHOOK_SECRET` se cargan a mano en
Render (`sync: false`).

## Jobs programados (Render Cron)

Tareas que corren **fuera** del proceso web, en un **Cron Job de Render** (`type: cron` en
`render.yaml`): reusa el **mismo Docker image**, arranca en horario, corre un comando y termina
(no es un scheduler in-process; no toca el web). Se ejecutan on-demand con **"Trigger Run"** en
el dashboard.

- **`scripts/notify_expiring.py`** — aviso de **vencimiento próximo** de tiers pagos. Cron
  **diario** (`schedule: "0 12 * * *"` = 12:00 UTC ≈ 09:00 AR), comando
  `python -m scripts.notify_expiring`. Busca tiers `mensual`/`anual` **vigentes** que vencen
  dentro de `TIER_EXPIRY_WARNING_DAYS` (default **10**) y **sin aviso previo**
  (`UserTier.expiry_warning_sent_at IS NULL`), manda `send_tier_expiring_email` (CTA → `/pricing`)
  y estampa la marca. **Idempotente**: no reenvía al día siguiente. La marca se **limpia**
  (`= None`) al renovar/pagar (`activate_paid_tier`) y al cambiar tier a free (`set_user_tier`),
  así el nuevo período vuelve a avisar (migración `0010`). Flag **`--dry-run`** para listar sin
  enviar ni estampar. **Costo**: el cron se factura aparte del web, solo por el tiempo que corre
  (segundos/día → centavos); no consume ni reemplaza la instancia web.

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
- **Anti-bot / captcha — decisión (2026-07-24): NO se usa reCAPTCHA.** Las capas actuales
  alcanzan: **OTP por email** en login (2FA; brute-force de la password no sirve), **rate
  limiting** slowapi por `CF-Connecting-IP` (no spoofable) en los 16 endpoints, y **Cloudflare**
  (rate-limit 5/10s en `/auth/login` + Managed Challenge + `not cf.client.bot`).
  `forgot-password`/`resend-otp` devuelven 204 (no enumeran cuentas). **Gatillo para agregar
  captcha**: abuso real en los endpoints que **mandan emails** (`forgot-password`/`resend-otp`
  → mail-bombing a una víctima o quemar la cuota de Resend), donde el rate limit **por IP** no
  frena una botnet distribuida. En ese caso usar **Cloudflare Turnstile en modo INVISIBLE** (NO
  Google reCAPTCHA: ya estás en Cloudflare, es gratis y privacy-friendly; y el front prohíbe
  campos visibles extra en las pantallas de auth, ver `3D/CLAUDE.md`) y **validar el token en el
  backend** (un captcha solo en el front es bypasseable; el guard de origen NO protege endpoints
  públicos de forja). Hueco conocido: el Managed Challenge de Cloudflare está acotado a **fuera
  de LATAM** → no desafía bots locales (AR/LATAM).

## Despliegue / SameSite

`SameSite=Lax` solo manda la cookie en peticiones **same-site** (mismo dominio registrable).
- Dev (`localhost:3000` ↔ `localhost:8000`): same-site → `Lax`, sin `Secure`.
- Prod mismo dominio (`app.x.com` + `api.x.com`): `Lax` + `Secure`.
- Prod cross-site (`*.pages.dev` + `*.railway.app`): `COOKIE_SAMESITE=none` (fuerza `Secure`)
  + `CORS_ORIGINS` con el origen exacto, todo HTTPS.

El front debe llamar a la API con `credentials: "include"` para enviar/recibir la cookie.

## Producción (Cloudflare + Vercel + Render + Neon)

Topología: visitante → **Cloudflare** (DNS + proxy + WAF) → **Vercel** (front `www.corpolab3d.com`)
/ **Render** (backend `api.corpolab3d.com`). DB en **Neon** (solo accesible por `DATABASE_URL`
desde Render). Cookie **host-only** (`COOKIE_DOMAIN` ausente a propósito), `COOKIE_SAMESITE=lax`
(www y api son same-site).

- **Render deploya desde `main`, NO desde `dev`.** `dev` = staging; se promueve con el workflow
  **manual** de GitHub Actions **"Promote dev to main"** (Actions → Run workflow): corre CI sobre
  `dev` (compila + importa la app) y si pasa mergea `dev`→`main` y pushea (dispara el CD de
  Render). **No promover a mano** (un commit local se cuela a main). `promote.yml` vive **solo en
  `main`** (rama default) para que aparezca el botón.
- **Cloudflare WAF (plan Free)** — reglas activas (2026-07-24):
  - Custom rule 1 (Skip): `/.well-known/` + `/payments/webhook` → saltea todo el WAF (protege la
    renovación del cert y el webhook de MP).
  - Custom rule "Admin solo Argentina" (Block): `/admin` o `/ingresar` con `ip.src.country ne "AR"`.
  - Custom rule "Challenge fuera de LATAM" (Managed Challenge): acotada a `http.host eq
    "www.corpolab3d.com"` (NO api, o rompería los fetch del front con challenge) y `not cf.client.bot`.
  - Rate limiting rule (0/1 del Free): `/auth/login` POST, 5/10s → Block (borde).
  - Transform Rule: inyecta `x-origin-secret` en `api.corpolab3d.com` (el guard de origen).
  - Cupos: custom 3/5 · rate-limiting 1/1 · transform 1/10.

## Correo del dominio (corpolab3d.com)

Setup hecho el **2026-07-24**. Dos piezas **separadas** que conviven sin pisarse (recibir usa
**MX en la raiz**; enviar usa **TXT + MX en el subdominio `send`**).

**Recepcion** → **Cloudflare Email Routing** (gratis en todos los planes; solo **reenvia**, no da
casilla propia). **HECHO y probado** (llegan los mails):
- `info@corpolab3d.com` y `support@corpolab3d.com` → reenvian a `leocueto1999@gmail.com`.
- **Catch-all activo** → misma bandeja (cubre typos: `suport@`, `ifno@`, etc.).
- El "Email Sending" nativo de Cloudflare NO se usa (Beta + exige plan Workers Pago).

**Envio (app)** → **Resend** (ya integrado en `app/email.py`: reset password + OTP). Dominio
`corpolab3d.com` **verificado**. Sender **`no-reply@corpolab3d.com`** via `EMAIL_FROM` (default
igual en `app/config.py`; en prod manda la env de Render).

**Responder como info@/support@** → Gmail "Enviar como" via **SMTP de Resend**
(`smtp.resend.com:465`, user `resend`, pass = API key de Resend).

Registros DNS en Cloudflare (todos **DNS-only** / nube gris; MX y TXT ni se proxean):

| Origen | Type | Name | Value (resumen) |
| --- | --- | --- | --- |
| Email Routing | MX ×3 | `corpolab3d.com` (raiz) | `route1/2/3.mx.cloudflare.net` |
| Email Routing | TXT (SPF) | `corpolab3d.com` (raiz) | `v=spf1 include:_spf.mx.cloudflare.net ~all` |
| Email Routing | TXT (DKIM) | `cf2024-1._domainkey` | firma del reenvio |
| Resend | TXT (DKIM) | `resend._domainkey` | `p=MIG...` |
| Resend | MX | `send` | `feedback-smtp...amazonses.com` (prio 10) |
| Resend | TXT (SPF) | `send` | `v=spf1 include:amazonses.com ~all` |
| Resend | TXT (DMARC) | `_dmarc` | `v=DMARC1; p=none;` |

> **No pisar el SPF de la raiz** (Email Routing): el SPF de Resend vive en el subdominio `send`,
> no en la raiz. Con el dominio verificado en Resend ya **no hay bloqueo tecnico para activar el
> OTP de login** (`OTP_ENABLED`); esa activacion queda como decision aparte. DMARC arranca en
> `p=none` (monitoreo) y se endurece luego. El WAF no interviene (el mail va por MX/SMTP, no HTTP).

**Estado (2026-07-24):** recepcion (info/support/soporte/contacto/ventas + catch-all APAGADO →
las inexistentes rebotan) **probada OK**. Dominio en Resend **verificado**. `EMAIL_FROM` en Render
= `no-reply@corpolab3d.com`. **Envio real VERIFICADO**: un `forgot-password` de prod llego
`From: no-reply@corpolab3d.com` con **SPF+DKIM+DMARC = PASS**. Frontend actualizado: landing y
paginas legales apuntan a `contacto@corpolab3d.com` (rama `fix/contacto-emails` mergeada a `dev`).
**Pendiente** (ver "TODO / pendiente"): Gmail "Enviar como" para responder desde los alias. Los
templates branded de los mails (header/footer + tema claro, Jinja2 en `app/mailing/`) ya están
**HECHOS** (2026-07-24); falta solo la verificación visual en Gmail/Outlook post-deploy de los PNG.

## Convenciones / cuidados

- **Respuestas al usuario (chat)**: español SIN tildes/acentos (ej. "Confirmas"). Solo el chat,
  no el código ni estos docs.
- Lógica de validación de sesión: **una sola** (`get_current_user`); no duplicarla por endpoint.
- Cambios de schema → nueva migración Alembic en `alembic/versions/`, no editar la `0001`.
- `psycopg[binary]` en local (Windows/glibc) y en el contenedor (hay wheels musllinux; trae
  `libpq` embebida — psycopg puro falla en Alpine por `ctypes.find_library`).

## TODO / pendiente

- **Correo — Gmail "Enviar como" (Parte D del setup de correo): PENDIENTE.** Configurar en Gmail
  el "Enviar como" para **responder** desde `info@`/`support@`/`contacto@` (que el cliente vea la
  respuesta desde la direccion de la empresa, no desde el Gmail personal). Datos del SMTP de
  Resend: servidor `smtp.resend.com`, puerto `465` (SSL), usuario `resend`, contraseña = una
  **API key de Resend** (conviene una dedicada, ej. `gmail-smtp`, para poder revocarla sola). El
  codigo de confirmacion de Gmail llega solo porque esos alias ya reenvian al Gmail (Parte A).
  Repetir por cada direccion. Recepcion + envio ya funcionan; esto es solo para responder-como.
- **Correo — templates branded de los mails transaccionales: HECHO (2026-07-24).** Sistema de
  templates en **`app/mailing/`** con **Jinja2** (dep `jinja2` en ambos requirements). Estructura:
  `render.py` (`render_email(template, **ctx)`, autoescape ON, inyecta `assets_base_url` +
  `support_email`) + `templates/` con `base.html` (card centrada, tema **claro** CorpoLab) que
  incluye `header.html` (logo-texto arriba-izq) y `footer.html` (logo-completo abajo-izq +
  "¿Necesitas ayuda?" + `info@corpolab3d.com`); cada mail **extiende** `base.html`:
  `reset_password.html`, `signup_verification.html`, `login_otp.html` y (2026-07-25)
  `welcome.html`, `payment_approved.html`, `payment_rejected.html`, `tier_expiring.html`.
  `app/email.py` ahora
  **renderiza con Jinja2** (helper `_send` a Resend; se eliminó el HTML inline y el `str.replace`;
  se borró `app/templates/otp.html`). **Logos**: los mails apuntan al **CDN del front**
  (`{FRONTEND_URL}/logo/logo-texto.png` y `logo-completo.png`, PNG servidos por Vercel/Cloudflare;
  se convirtieron de `.webp` porque varios clientes de email no renderizan webp). Copia fuente de
  los PNG en `app/mailing/assets/`. **Pendiente visual**: verificar el render real en Gmail/Outlook
  una vez deployados los PNG en `corpolab3d.com/logo/` (el front tiene que estar publicado).
- Conectar el frontend Nuxt (página `/login`, middleware de auth, composable `useAuth`,
  capa de servicio con `credentials: "include"` y el fetching nativo de Nuxt 4).
- Limpieza de filas vencidas (job periódico o `DELETE` de paso en login/signup): aplica a
  `sessions`, `password_reset_tokens`, `login_otps` y `pending_registrations` (todas guardan
  `expires_at`). Se resuelve con un `DELETE ... WHERE expires_at < now()`; **NO** justifica traer
  Redis (ver la nota de Redis). Ya hay infra de **Cron Job de Render** (ver "Jobs programados") →
  este cleanup puede colgarse ahí como otro script diario cuando se implemente.
- **Redis — cuándo SÍ, cuándo NO (decisión 2026-07-24, a futuro):** hoy **no se usa** y no hace
  falta (single-instance, "pocos usuarios internos"). Todo el estado efímero (sesiones, tokens de
  reset, OTP, pending registrations) vive en **Postgres a propósito**, por la **atomicidad
  transaccional** (ej. `verify-signup` crea el `User` y marca el pending usado en una sola
  transacción; con Redis se partiría en dos datastores y se perdería). Distinguir los dos roles de
  Redis para no mezclarlos:
  - **Store efímero / rate-limit distribuido** → el gatillo real es correr **múltiples instancias**
    del backend: ahí el `slowapi` en memoria (1 instancia) deja de servir y Redis pasa a ser
    necesario para el rate-limit compartido (y opcionalmente sesiones/tokens, asumiendo el
    trade-off de atomicidad). Mientras sea 1 instancia, no aporta.
  - **Caché** → solo para respuestas **dinámicas del backend, caras y compartidas** (ej.
    `GET /plans`, agregados). **NO** para la landing ni contenido estático: la landing es una **SPA
    estática** servida por **CDN (Vercel) + Cloudflare** en el borde — eso ya es más rápido y
    barato que Redis. Para público estático/casi-estático, cachear en **Cloudflare/CDN
    (`Cache-Control`)**, no en Redis.
- Rate limiting (hecho): `slowapi` por IP en 16 endpoints (`auth.py` login/register/signup/
  OTP/reset, `designs.py` list/write/open/thumb/save/delete); limiter en `app/ratelimit.py`.
  **Keyeado por `CF-Connecting-IP`** (no por `get_remote_address` a secas): detrás de
  Cloudflare, uvicorn toma el primer valor de `X-Forwarded-For`, que Cloudflare **appendea**
  (no reescribe) → un atacante que manda su propio `X-Forwarded-For` controla ese valor y
  saltea el límite. `CF-Connecting-IP` Cloudflare lo **sobrescribe siempre** (no spoofable);
  con fallback a `get_remote_address` en dev local. Verificado en prod (2026-07-23) que el
  header sobrevive intacto los **dos** Cloudflares de la cadena (el tuyo + el de Render). En
  memoria (1 instancia); para multi-instancia haría falta Redis. Complemento en el **borde
  (HECHO 2026-07-24)**: Rate limiting rule de Cloudflare 5/10s en `/auth/login` (es Rate
  limiting rule, NO custom rule; con Block plano bloquearías TODOS los logins). Verificado:
  el backend corta al 6º request (429 slowapi), Cloudflare al 7º (429 error 1015, en el borde).
- **Login con Google (OIDC): NO implementado / LATENTE.** El código del backend ya existe
  (`app/google_oauth.py`, endpoint `/auth/google`, migración `0007`, columnas
  `google_sub`/`auth_provider`) pero no está activo end-to-end: el front no ofrece SSO y sin
  `GOOGLE_CLIENT_ID` el endpoint responde 401. Queda como base para el día que se active
  (ver flujo de auth #6). Todos los users hoy son `auth_provider='password'`.
- **Guard de origen: ACTIVO en prod** (2026-07-23). `ORIGIN_SECRET` cargada en Render;
  verificado: `/health` directo = 200 · `/auth/me` directo (onrender.com) = 403 · `/auth/me`
  por Cloudflare (api.corpolab3d.com) = 401 (guard transparente para el tráfico legítimo).
- **Pagos MercadoPago (Checkout Pro): webhook PROBADO, falta activar producción.** Ver la
  sección "Pagos (MercadoPago)". La firma HMAC del webhook quedó verificada (la simulación
  desde el panel de MP da 200). El día que se lance: activar Credenciales de producción en MP,
  cambiar `MP_ACCESS_TOKEN` al de producción, rotar `MP_WEBHOOK_SECRET` (se filtró en debug) y
  probar un pago real chico.
- **Servir estáticos desde un bucket de estáticos: PENDIENTE (a futuro).** Mover los assets
  estáticos a un **bucket de object storage** (ej. Cloudflare R2 / S3) servido por CDN, en vez
  de servirlos desde el backend. Aplica a lo estático que hoy pueda estar saliendo por el
  proceso FastAPI (ej. los PNG de logos de los mails en `app/mailing/assets/`, o cualquier
  archivo público). Definir el bucket, el dominio/CDN que lo expone y actualizar las URLs
  (`assets_base_url` / `FRONTEND_URL`) que apuntan a los assets.

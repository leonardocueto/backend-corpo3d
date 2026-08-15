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

- **User** ganó `deactivated_at` (nullable, migración `0016`): fecha en que el usuario pidió la
  baja desde `POST /auth/deactivate`. Va **aparte de `is_active`** a propósito — el bool es el
  interruptor que corta el acceso, esta columna es la **constancia** de cuándo entró el pedido
  (sin fecha no se sostiene el plazo del art. 16 de la Ley 25.326). `NULL` + `is_active=False` =
  la desactivó un admin, no el usuario. Se limpia al reactivar desde el panel.
- **User**: `id` (UUID), `email` (único), `full_name`, `password_hash` (**nullable**: los
  usuarios creados por Google no tienen password), `is_active`, `is_admin`, `google_sub`
  (**único, nullable**: `sub` estable de la cuenta de Google para linkeo), `auth_provider`
  (`'password' | 'google'`, default `'password'`), `must_change_password` (default `false`,
  migración `0013` — ver abajo), `terms_accepted_at` + `terms_version` (nullable, migración
  `0014` — ver abajo), `created_at`. (Columnas de Google: migración
  `0007_google_oauth`; **latentes**, hoy todos los users son `auth_provider='password'` — el login
  con Google no esta activo, ver flujo de auth #6.)
- **Session**: `id` (UUID), `user_id` (FK, `ON DELETE CASCADE`), `token_hash` (único),
  `expires_at`, `created_at`, `revoked_at` (nullable). Borrar un User borra sus sesiones.
- **Subscription** (migración `0011`): `id` (UUID), `user_id` (FK CASCADE), `plan`
  (`"mensual"` | `"anual"`), `mp_preapproval_id` (UNIQUE — idempotencia), `mp_status`
  (`"pending"` | `"authorized"` | `"paused"` | `"cancelled"`), `mp_payer_id` (nullable),
  `created_at`, `updated_at`. Múltiples filas por usuario (cancel + re-subscribe).
- **ExportLog** (migración `0011`): `id` (UUID), `user_id` (FK **SET NULL**, nullable —
  migración `0012`), `payment_id` (FK SET NULL, nullable — último pago aprobado al momento del
  download), `r2_key` (`"exports/{user_id}/{id}.txt"`), `created_at` (indexed — para cleanup
  cron). Borrar un User deja los logs con `user_id=NULL`.
- **Payment** ganó `subscription_id` (FK `subscriptions.id` ON DELETE SET NULL, nullable) —
  distingue pagos de suscripción de legacy one-time. `user_id` pasó a FK **SET NULL** + nullable
  (migración `0012`): borrar un User deja sus pagos con `user_id=NULL` (auditoria). `GET /payments`
  usa outer join; filas huérfanas muestran "Usuario eliminado" en el panel admin.
- **UserTier** ganó `subscription_id` (FK `subscriptions.id` ON DELETE SET NULL, nullable) —
  linkea la suscripción activa que alimenta el tier.
- **WithdrawalRequest** (migración `0015`): `id` (UUID), `full_name`, `email` (indexado, **no**
  único), `reason` (nullable — el art. 34 permite revocar sin justificar), `created_at`
  (indexed), `resolved_at` (nullable, lo estampa un admin). **Sin FK a `users`** (mismo criterio
  que `PendingRegistration`): el endpoint es público y quien revoca puede no tener sesión.

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
   - **Exige `accepted_terms: true` Y `accepted_privacy: true`** (uno por documento, migración
     `0014`): sin el campo o con `false` → **422**, con el mensaje del documento que falta. En
     `SignupIn` van como `Field(default=False, validate_default=True)`: **el `validate_default`
     es imprescindible**, pydantic v2 **no corre los validators sobre los defaults** y omitir el
     campo se salteaba el guard (medido: respondía 202). **Las VERSIONES no las manda el
     cliente**: las estampa el servidor — un string de versión que sale del navegador no prueba
     nada. Se guardan en el `PendingRegistration` y de ahí se copian al `User`. Detalle en
     "Aceptación de los legales".
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
   - **Aceptación de legales**: se copian las cuatro columnas del pending tal cual, **sin
     re-estampar la fecha**. Lo que vale como aceptación es el momento del **clic en el
     registro**, no el de la confirmación del mail (que puede ser una hora después).
5c. `POST /auth/accept-legal`: acepta los legales desde `/politicas` (cuentas anteriores al
   checkbox, altas hechas por admin, o subidas de versión de un documento). Usa
   `get_current_user` y **no** `require_legal_acceptance` — exigir la aceptación para poder
   aceptar sería un deadlock. Sella la versión vigente **solo de los documentos que vienen en
   `true`**; devuelve el `UserOut` actualizado para que el front refresque su store sin una
   segunda llamada a `/auth/me`. Idempotente. Rate limit 10/min.
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
   - **Admins: OTP SIEMPRE obligatorio (2026-08-10), sin importar `OTP_ENABLED`.**
     `_otp_required(user)` en `app/routers/auth.py` es `settings.otp_enabled or user.is_admin`:
     `OTP_ENABLED` solo gobierna a los usuarios NO admin; un `is_admin=True` pasa por los 2 pasos
     aunque el switch global esté en `false`. Motivo: los admins crean usuarios, mueven tiers y
     ven pagos — una password admin filtrada sin 2do factor es acceso total. Consecuencia de
     infra: **Redis pasa a ser dependencia dura del login admin**, no solo del switch global; sin
     `REDIS_URL` el login de un admin responde **503** (antes era inalcanzable porque el boot
     guard de `app/redis.py` mataba el proceso si `OTP_ENABLED=true` sin Redis; ahora con el
     switch en `false` la app arranca igual y recién falla en ese login puntual — `app/redis.py`
     loguea un warning al arranque cuando falta `REDIS_URL`). `docker-compose.yml` suma un
     servicio `redis:7-alpine` para poder probar el flujo en local.
   - **`resend-otp` tambien filtra por `_otp_required`**: solo emite codigos para usuarios que
     realmente pasan por OTP (sigue respondiendo 204 para el resto, anti-enumeracion). Si no,
     con `OTP_ENABLED=false` el endpoint seria un "mandale un mail a esta direccion" **sin
     autenticacion** para cualquier cuenta activa, cuando esos usuarios ni siquiera llegan a la
     pantalla de verificacion.
   - **`POST /auth/resend-otp`** (botón "reenviar"): responde **SIEMPRE 204** (anti-enumeración).
     **Cooldown server-side**: NO emite un código nuevo mientras el usuario tenga uno **activo**
     (sin usar y sin vencer) — es la validación real del timer del front (que solo habilita
     "reenviar" al vencer el código), no salteable manipulando el cliente. Al vencer el código
     actual, el reenvío vuelve a estar disponible. (Además hay rate-limit slowapi 1/min por IP,
     que el cooldown de 3 min ya subsume.)
8. **`POST /auth/deactivate`: baja self-service** (2026-08-14, rate-limit 3/min). Es un **borrado
   BLANDO**, no un `DELETE`: pone `is_active=False`, estampa `deactivated_at`, cancela la
   suscripción en MP y revoca todas las sesiones.
   - **Por qué blando**: `payments` y `export_logs` referencian al usuario. Un hard delete los
     deja con `user_id=NULL` y se pierde a quién correspondía cada cobro, que es justo lo que la
     normativa fiscal obliga a conservar. El corte de acceso es igual de inmediato: `deps.py`
     valida `is_active` en **cada** request, así que la sesión muere en el siguiente llamado.
   - **Usa `get_current_user`, NO `require_legal_acceptance`** — mismo criterio que `/logout` y
     `/accept-legal`: exigir la aceptación de un contrato para poder irse sería coercitivo. Un
     usuario trabado en `/politicas` **tiene** que poder darse de baja (verificado: 204, no 403).
   - **Cancelar la suscripción es obligatorio**: `is_active=False` no toca la preapproval, así que
     sin eso MP le **sigue cobrando** a alguien que ya se fue. Es **best-effort**: si MP no
     responde, la baja se persiste igual (revertirla por un fallo de un tercero sería peor) y
     queda un log con "CANCELARLA A MANO".
   - **La baja se revierte SOLO desde el panel admin** (`PATCH /users/{id}` con
     `is_active: true`, que además limpia `deactivated_at`). Es imprescindible que exista: la
     fila sigue ocupando el email (único), así que esa persona no puede entrar **ni volver a
     registrarse**. Ver el TODO del `CLAUDE.md` raíz.
   - **No pide la contraseña** para confirmar (decisión de producto 2026-08-14). El modal vive
     detrás de una cookie viva; el riesgo asumido es una sesión abierta ajena.

## Claves temporales del panel admin (`must_change_password`)

Cuando un admin da de alta (o resetea) una cuenta desde el panel puede pedirle al front que
**genere** la contraseña (12 chars, `crypto.getRandomValues`, ver `3D/app/utils/password.ts`).
Esa clave es **nuestra**, no del usuario: se la mandamos por mail y tiene que reemplazarla.

- **Columna** `users.must_change_password` (bool, default `false`, migración `0013`).
- **La prende** el panel: `POST /users` y `PATCH /users/{id}/password` la reciben en el body
  (`must_change_password`, default `false` → los payloads viejos siguen andando igual).
  **No se prende sola en ningún otro camino**: signup, Google y reset-password nunca la setean.
- **La apagan** `POST /auth/change-password` y `POST /auth/reset-password`, o sea los dos
  lugares donde la clave pasa a ser elegida por el usuario. Que reset-password también la baje
  no es cosmético: sin eso, alguien que recibió una clave del panel y después usó "olvidé mi
  contraseña" quedaría trabado en `/cambiar-password` para siempre.
- **Viaja en `UserOut`**, así que la ven `/auth/login`, `/auth/verify-otp` y `/auth/me`.
- **El bloqueo es del front**, no del backend (decisión 2026-08-10): el middleware global de
  Nuxt no deja salir de `/cambiar-password` mientras el flag esté prendido, pero los endpoints
  privados **no** devuelven 403. El riesgo es nulo — el usuario ya se autenticó con una clave
  que le dimos nosotros, y lo que se busca es que elija una propia, no contener a un atacante.
  Si algún día hace falta endurecerlo, va como dependency en `deps.py`, no endpoint por endpoint.

## Aceptación de los legales (migración `0014`)

Evidencia de que el usuario **vio y aceptó** los Términos y la Privacidad. No es cosmético: los
Términos §1 afirman que crear una cuenta implica aceptarlos, y las cláusulas que más protegen
(§12 validación de fabricación, §15 limitación de responsabilidad) son **inoponibles si no se
puede probar que se exhibieron**. Antes de la `0014` el registro no mostraba ni exigía nada, así
que esa aceptación no existía en ningún lado.

**Cuatro columnas, dos por documento**: `terms_accepted_at` / `terms_version` y
`privacy_accepted_at` / `privacy_version` (en `users` y en `pending_registrations`). Se guarda la
**versión aceptada, no un bool** — eso es lo que permite pedir la re-aceptación cuando el
documento cambia, sin backfill ni job.

- **Estado derivado, no almacenado**: `User.terms_accepted` / `privacy_accepted` (propiedades en
  `models.py`) comparan la versión guardada contra la vigente. Una versión vieja da `False` y el
  usuario vuelve a pasar por `/politicas`. **Es la única definición de "aceptó"**: la usan tanto
  `UserOut` (lo que ve el front) como el 403 del backend, así que no pueden opinar distinto.
- **Dos versiones separadas** (`TERMS_VERSION` / `PRIVACY_VERSION` en `config.py`, ambas
  `"2026-08-07"`): los documentos cambian por su cuenta y subir uno solo tiene que pedir la
  re-aceptación de ese, no de los dos. **Las pone el servidor**, nunca el cliente. Tienen que
  coincidir con las del front (`3D/app/utils/legal.ts`), que son las que se imprimen en
  `/terminos` y `/privacidad`. **Si se actualiza un documento, se suben las dos.**
- **Dónde se sella**: `POST /auth/signup` (el clic de los checkboxes; viaja por
  `PendingRegistration` y `/auth/verify-signup` lo copia **sin re-estampar la fecha**) y
  `POST /auth/accept-legal` (la pantalla `/politicas`, para cuentas que ya existían, altas de
  admin, y subidas de versión). `accept-legal` solo toca los documentos que vienen en `true`:
  aceptar uno deja el otro pendiente, y el usuario sigue bloqueado.
- **ENFORCEMENT REAL: `require_legal_acceptance` en `deps.py` → 403 `legal_acceptance_required`.**
  El middleware del front que manda a `/politicas` es solo UX — vive en el cliente, así que un
  override de la respuesta de `/auth/me` (o pegarle a la API directo con la cookie) lo saltea.
  El 403 no tiene nada que overridear: el estado se deriva en el servidor de lo que hay en DB.
  Mismo criterio que el gate de `/exports/download`.
  - `require_admin` y `get_paid_user` **cuelgan de esta dependency**, no de `get_current_user`,
    para que el gate se herede solo en todo endpoint admin o de cuenta paga. Los admins **no**
    están exentos.
  - En `payments.py` y `exports.py` reemplaza a `get_current_user` endpoint por endpoint (ese
    router tiene endpoints públicos: `/plans`, `/webhook`, `/withdrawal`).
  - **Quedan afuera a propósito**: `/auth/me` (el front necesita leer los flags), `/auth/logout`
    (poder salir siempre), `/auth/accept-legal` (sería un deadlock) y `/auth/change-password`
    (una clave temporal de admin se cambia primero).
- **Valores posibles de las columnas `*_version`**:
  - la vigente → aceptación real, con su fecha.
  - `"legacy-backfill"` → cuenta anterior al checkbox. La `0014` la dio por aceptada con
    `*_accepted_at = created_at`. Es una aceptación **asumida por uso previo**, no un clic
    registrado; se marca distinto a propósito (un registro que afirmara lo contrario sería peor
    evidencia que ninguno). Al no coincidir con la vigente, igual pasa por `/politicas`.
  - `NULL` → **no consta**. Altas hechas por admin (`/auth/register`, `POST /users`): no hay nadie
    del otro lado aceptando. También caen en `/politicas` al entrar. El panel las muestra en rojo.
- **Salida**: los **bools derivados** van en `UserOut` (los necesita el front para saber a dónde
  mandar al usuario y qué checkbox mostrar); la **evidencia cruda** (fecha + versión aceptada de
  cada documento) va solo en `AdminUserOut`.

## Exportaciones — gate de descarga (`app/routers/exports.py`)

**`POST /exports/download` es el punto de enforcement REAL de la descarga** (agregado el
2026-07-31, rama `feature/export-download-gate`). Recibe por **multipart** los archivos que
generó el editor (`files[]`, cada uno con su path relativo como `filename`, ej.
`cuerpo/pieza-1.stl`), más `structure_id` y `project_name`; valida server-side y devuelve el
**ZIP armado acá** (`zipfile` en memoria).

- **Por qué existe**: el ZIP se armaba 100% en el navegador y el backend solo se consultaba
  (llamada cooperativa que el cliente podía saltear). Con un *override* de la respuesta de
  `GET /exports/attempts` (`unlimited: true`) el cliente se auto-habilitaba descargas
  ilimitadas y estructuras premium. Ahora **la response ES el artefacto**: sin pasar la
  validación no hay archivo, y un override de responses no puede fabricar el ZIP.
- **Orden de validación** (importa): (1) `structure_id ∈ PREMIUM_STRUCTURE_IDS` y usuario no
  ilimitado → **403 `detail="premium_structure"`** (detail distinguible para que el front abra
  el modal de upsell y no el de "sin intentos"); (2) sanitización del payload (**antes** de
  debitar, así un 4xx no consume intento): sin traversal ni paths absolutos, extensiones en
  `ALLOWED_EXPORT_EXTENSIONS`, tope `MAX_EXPORT_FILES`/`MAX_EXPORT_BYTES`; (3) **filtro del
  PDF** para cuentas free (la ficha técnica es feature paga); (3b) **watermark del PNG** para
  cuentas free (Pillow: `_watermark_png`, semi-transparente bottom-right, 4% de la altura;
  el front también aplica el suyo — el backend refuerza); (4) free → `_consume_attempt`.
- **`_consume_attempt(db, user_id, now)`**: descuenta bajo `SELECT ... FOR UPDATE` **sin
  commitear** — el commit ocurre recién con el ZIP ya armado, así el debit y la entrega son
  **atómicos** (si el armado falla, el rollback devuelve el intento).
- `PREMIUM_STRUCTURE_IDS` guarda **solo los IDs**: las definiciones geométricas se quedan en el
  front a propósito, para que un Free pueda **previsualizar y editar** estructuras premium
  (funnel de venta). Lo que se corta es la **descarga**, no el preview. Si se agrega una
  estructura premium al catálogo del front, **agregar su id acá también**.
- **Residuo aceptado**: los adapters STL/DXF viven en el bundle (los necesita el preview), así
  que scripting activo en consola puede rearmar un ZIP local. Cubrimos el ataque por override
  de responses, que es el realista.
- **Audit log de exportaciones**: cada descarga exitosa (incluido admin) registra un `ExportLog`
  en DB + sube un `.txt` con JSON a R2 (`exports/{user_id}/{log_id}.txt`) en **BackgroundTask**
  (no bloquea la response). El `.txt` contiene `payment_id` (último pago aprobado, nullable),
  `user_id`, `project_name`, `structure_id`, `file_count`, `generated_at`, `downloaded_at`.
  Solo visible para admin/interno. El ExportLog en DB es la fuente de verdad; el .txt en R2 es
  best-effort (fallo del upload se loguea y se traga). Retención: 6 meses (cron diario
  `cleanup_export_logs.py` borra los viejos).

## Pagos (MercadoPago — Checkout Pro + Suscripciones)

`app/routers/payments.py`. Dos modalidades coexistentes:

### Legacy — Checkout Pro (pago único)

`sdk.preference().create(...)` → devuelve `init_point`, el front redirige a la página de MP.
`POST /payments/checkout` (DEPRECATED — se mantiene mientras el front viejo siga en prod).

### Suscripciones — Preapproval API (débito automático)

`sdk.preapproval().create(...)` con `status: "pending"` → MP muestra su página hosted donde el
usuario autoriza el débito automático. **Standalone** (sin `preapproval_plan`): no requiere
`card_token_id` desde el front.

- `POST /payments/subscribe`: crea la preapproval en MP + inserta `Subscription` en DB → retorna
  `init_point`. Guard: 409 si ya hay suscripción `authorized`.
- `GET /payments/subscription`: suscripción activa del usuario (non-cancelled más reciente) o
  `null`.
- `POST /payments/cancel-subscription`: cancela en MP + deactiva el tier.

### Invariantes comunes

- **Anti-tamper**: el cliente manda solo `{ plan }`; el **monto lo fija el servidor** (precios
  en `config`). `GET /plans` expone los precios (la UI los lee de ahí, nada hardcodeado).
- **El tier se activa SOLO desde el webhook con firma validada** (`POST /payments/webhook`),
  nunca desde el redirect del navegador. Idempotente por `Payment.mp_payment_id` UNIQUE.
- **Firma del webhook** (`_valid_signature`): HMAC-SHA256 con `MP_WEBHOOK_SECRET`. Sin secret o
  firma que no matchea → **401**. Misma firma para todos los topics.
- **Webhook 3 topics**: `payment` (legacy one-time), `subscription_preapproval` (cambio de
  estado de la suscripción: authorized/paused/cancelled), `subscription_authorized_payment`
  (cobro recurrente: approved/rejected).
- **Tier dual-authority**: `Subscription.mp_status == "authorized"` (primario) +
  `UserTier.expires_at > now` (fallback para legacy one-time y safety net por webhook que no
  llega). `user_is_unlimited()` chequea ambos: suscripción primero, expiry después.
- **Coexistencia**: pagos legacy (`subscription_id = NULL`) siguen funcionando por `expires_at`.
- **Mails de suscripción** (5 templates Jinja2): activated, cancelled, paused, charge_failed y
  **refund_processed** (`send_refund_email`, una sola plantilla que cubre reembolso y contracargo
  vía el flag `is_chargeback`; se encola DESPUÉS del commit de la revocación).
- **Mails de resultado (legacy)**: approved → activa tier; rejected/cancelled → registra el
  status + email.
- **Cancelar ≠ reembolsar (2026-08-09; endpoint alineado y auto-cancel en reembolso 2026-08-10).**
  Son dos efectos distintos sobre el tier:
  - **Cancelada o pausada** → corta los cobros **futuros**, pero **el tier pago se conserva hasta
    `expires_at`**: el período en curso ya se cobró y no se devuelve. Vale **tanto** para la
    cancelación desde el panel de MP (webhook `subscription_preapproval` →
    `_handle_subscription_status`) **como** para la cancelación desde la app
    (`POST /payments/cancel-subscription`): **ninguno** llama a `deactivate_subscription_tier`
    (el endpoint dejó de hacerlo el 2026-08-10, antes bajaba el tier en el acto e iba
    inconsistente con el webhook). El downgrade lo hace solo `sync_user_tier` →
    `downgrade_if_expired` cuando vence. `user_is_unlimited` lo sostiene por el fallback
    `tier_is_unlimited` (la suscripción ya no está `authorized`, pero el tier sigue siendo pago y
    vigente).
  - **Reembolsada o con contracargo** (`_apply_refund`, estados `refunded` / `charged_back`) →
    **revocación inmediata a free**, porque la plata volvió. Marca el `Payment` con el estado
    nuevo y llama a `deactivate_subscription_tier`, que además desengancha
    `tier.subscription_id` — así queda revocado incluso si la suscripción sigue `authorized` en
    MP. **Además cancela la suscripción en MP** (2026-08-10): reembolsar = el cliente se va, así
    que `_apply_refund` cancela la preapproval (`sdk.preapproval().update(..., cancelled)`) y
    marca la `Subscription` local `cancelled`, para que **no vuelva a cobrar el mes siguiente**.
    Ubica la sub por `payment.subscription_id` (fallback: la non-cancelled más reciente del
    usuario). Es **best-effort**: si el update a MP falla, se loguea pero NO rompe el webhook (la
    revocación del tier ya está persistida). Un reembolso de pago legacy one-time no tiene sub y
    salta el bloque.
  - **`_apply_refund` corre ANTES del guard de idempotencia** por `mp_payment_id` de los dos
    handlers de pago. Es obligatorio que sea así: MP re-notifica el reembolso con el **mismo**
    id de pago, y ese guard corta con `{"status": "ok"}` apenas encuentra la fila. Su
    idempotencia es por **estado** (`payment.status in _REFUND_STATUSES`), no por existencia.
  - Solo cubre reembolsos **totales**. Uno parcial deja el pago en `approved` con
    `status_detail=partially_refunded` y el usuario conserva el tier (devolviste una parte).
- **Reembolsos**: se hacen **manual desde el panel de Mercado Pago** (no hay endpoint en el
  backend). Decisión de producto (2026-08-01): mantenerlo manual mientras el volumen sea bajo.
  El webhook del reembolso está automatizado (ver arriba): reembolsás en MP y el tier cae solo
  **y la suscripción se cancela sola** (2026-08-10) — ya **no** hace falta cancelarla a mano
  como paso aparte; una sola acción (reembolsar) devuelve la plata, baja el tier y frena la
  renovación.
- **Botón de arrepentimiento** (Ley 24.240 art. 34 + Res. 424/2020): `POST /payments/withdrawal`
  (endpoint **público**, sin sesión, rate-limit 3/min). Recibe `full_name`, `email` y `reason`.
  El front tiene la página `/arrepentimiento` con formulario + links en el footer de la landing
  y en `/pricing`.
  - **La FILA en `withdrawal_requests` es el registro legal; el mail es solo la notificación**
    (migración `0015`, 2026-08-14). El orden está fijado y **no se invierte**: `db.commit()`
    **antes** de encolar `send_withdrawal_request_email`. Antes de esto el endpoint solo mandaba
    el mail y respondía 204 sin escribir nada; como `_send` traga los errores por diseño, una
    caída de Resend dejaba al cliente con un éxito falso y la solicitud sin rastro en ningún
    lado. Con un plazo legal de 10 días corridos, eso no es un mail perdido: es un
    incumplimiento que además no se puede auditar. Si el commit falla ahora propaga **500** y el
    cliente ve el error (reintenta) en vez del cartel de "listo".
  - **Panel admin**: `GET /payments/withdrawals` (paginado, filtro `pending`) y
    `PATCH /payments/withdrawals/{id}` (`{"resolved": bool}`; `false` **reabre** una cerrada por
    error). Ambos con `require_admin` a nivel **endpoint**, no de router — este módulo tiene
    rutas públicas. La página del front es `/admin/arrepentimientos` y **resalta las pendientes
    que pasaron los 10 días**.
  - **El reembolso y la cancelación de la suscripción siguen siendo manuales** en el panel de MP;
    resolver la solicitud solo cierra el registro. (Automatizar la cancelación al registrarla se
    evaluó y quedó fuera de alcance a propósito.)

**Estado (2026-07-24): legacy PROBADO, suscripciones por probar con credenciales de producción.**
`MP_ACCESS_TOKEN` y `MP_WEBHOOK_SECRET` se cargan a mano en Render (`sync: false`).

## Jobs programados (Render Cron)

Tareas que corren **fuera** del proceso web, en un **Cron Job de Render**: reusa el **mismo
Docker image**, arranca en horario, corre un comando y termina (no es un scheduler in-process;
no toca el web). Se ejecutan on-demand con **"Trigger Run"** en el dashboard. **Creados a mano
en el dashboard** (no via blueprint/`render.yaml` — se sacaron del yaml el 2026-08-04 para
evitar que Render los auto-recree en la región default al deployar). Todos en **Ohio (US East)**.

- **`scripts/notify_expiring.py`** — aviso de **vencimiento próximo** de tiers pagos. Cron
  **diario** (`schedule: "0 12 * * *"` = 12:00 UTC ≈ 09:00 AR), nombre en Render
  **`cron-corpolab3d-tier-expiry`**, comando `python -m scripts.notify_expiring`. Busca tiers
  `mensual`/`anual` **vigentes** que vencen dentro de `TIER_EXPIRY_WARNING_DAYS` (default **10**)
  y **sin aviso previo** (`UserTier.expiry_warning_sent_at IS NULL`), manda
  `send_tier_expiring_email` (CTA → `/pricing`) y estampa la marca. **Excluye usuarios con
  suscripción `authorized` activa** (MP les cobra solo, no necesitan aviso). **Idempotente**: no
  reenvía al día siguiente. La marca se **limpia** (`= None`) al renovar/pagar
  (`activate_paid_tier`) y al cambiar tier a free (`set_user_tier`), así el nuevo período vuelve
  a avisar (migración `0010`). Flag **`--dry-run`** para listar sin enviar ni estampar.
- **`scripts/cleanup_export_logs.py`** — limpieza de **logs de auditoría de exportaciones** >
  `export_log_retention_days` (default **180** = 6 meses). Cron **semanal** (`schedule: "30 13
  * * 0"` = domingos 13:30 UTC ≈ 10:30 AR), nombre en Render
  **`cron-corpolab3d-export-cleanup`**. Borra los `ExportLog` de la DB + sus `.txt` de R2
  (idempotente: key inexistente = no-op). Los `Payment` **NO se tocan** (quedan para siempre).
  Chunks de 500. Flag **`--dry-run`**. Env vars: `DATABASE_URL`, `R2_ACCOUNT_ID`,
  `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `SESSION_SECRET` (dummy —
  `app.config` lo exige).
- **`scripts/cleanup_expired.py`** — limpieza de **filas vencidas** de tablas efímeras:
  `sessions`, `password_reset_tokens`, `login_otps`, `pending_registrations` (todas con
  `expires_at < now()`). Cron **semanal** (`schedule: "0 13 * * 0"` = domingos 13:00 UTC ≈
  10:00 AR), nombre en Render **`cron-corpolab3d-expired-tokens-cleanup`**. No toca R2 ni manda
  mails. Flag **`--dry-run`**. Env vars: `DATABASE_URL`, `SESSION_SECRET` (dummy).
- **Costo**: cada cron se factura aparte del web, solo por el tiempo que corre (segundos/día →
  centavos); no consumen ni reemplazan la instancia web.

**Estado (2026-08-05):** los 3 crons **DESPLEGADOS en Ohio** (migrados desde Oregon junto con el
web). Los 3 probados OK (`tier-expiry`, `export-cleanup`, `expired-tokens-cleanup` — este último
verificado el 2026-08-05: 57 sessions, 7 reset tokens, 25 OTPs, 4 pending registrations eliminados).

- **Env vars del cron = MANUALES.** Es un **servicio aparte**: las vars (`DATABASE_URL`,
  `RESEND_API_KEY`, `EMAIL_FROM`, `FRONTEND_URL`, `SESSION_SECRET`) **NO se copian del web** — se
  cargan a mano en el dashboard del cron (Environment). Se copian **iguales** a las del web, salvo
  `SESSION_SECRET` que puede ser **cualquier random** (el cron no valida sesiones, pero
  `app/config.py` lo exige al importar o crashea). **Si cambia `DATABASE_URL`/`RESEND_API_KEY`,
  actualizar en TODOS los servicios (web + crons).**

**Facturación Render (aprendido 2026-07-25):** Render **NO** tiene un tope/alerta de **gasto
total** ("avisame/cortá al llegar a $X"): esa feature no existe. El único control es el **spend
limit del Build Pipeline** (Workspace Settings → Build Pipeline → Edit), que gobierna **solo
minutos de build/pre-deploy**, no el hosting ni el runtime del cron. Starter build: **$5/1.000
min, 500 min gratis/mes**. Está en **$0** a propósito → se usan los 500 gratis y, al agotarlos,
**los builds se frenan** (no cobra de más; ~2-5 min por build → los 500 alcanzan de sobra). El
hosting es suscripción fija (web Starter ~$7/mes) → gasto predecible, muy por debajo de $20. Un
tope **duro real** sobre el total solo se logra del lado del **medio de pago** (tarjeta con
límite).

## Seguridad — invariantes a NO romper

- El token plano vive **solo** en la cookie `HttpOnly`. En DB nunca el token plano, solo su
  HMAC (`String(64)` hex). El pepper es `SESSION_SECRET` (obligatorio, fuera del repo).
- **El login de un usuario `is_admin` nunca setea cookie en un solo paso.** Siempre pasa por el
  OTP de email (`_otp_required` en `app/routers/auth.py`), sin importar `OTP_ENABLED`. Si se
  toca `login()`, no reintroducir un camino que le dé cookie a un admin sin el 2do paso.
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

## Producción (Cloudflare + Render + Neon)

Topología: visitante → **Cloudflare** (DNS + proxy + WAF) → **Cloudflare Pages** (front
`www.corpolab3d.com`) / **Render** (backend `api.corpolab3d.com`). DB en **Neon** (solo accesible
por `DATABASE_URL` desde Render). Cookie **host-only** (`COOKIE_DOMAIN` ausente a propósito),
`COOKIE_SAMESITE=lax` (www y api son same-site). **Render y Neon co-ubicados en Ohio (US East)**
desde 2026-08-04 (antes: Render Oregon + Neon São Paulo, ~950 ms por endpoint; ahora ~200 ms).

> **El front se mudó de Vercel a Cloudflare Pages el 2026-08-13** (motivo: Vercel Free/Hobby no
> permite uso comercial). Para el backend **no cambió nada**: `www.corpolab3d.com` ya estaba en
> `CORS_ORIGINS` y sigue siendo *same-site* con `api.corpolab3d.com`, así que la cookie `lax` viaja
> igual. Detalle completo en el `CLAUDE.md` de la raíz → "Infra / hosting".
>
> **Cuidado si alguna vez se prueba el front en un `*.pages.dev`**: `pages.dev` es otro dominio
> registrable → el request a la API es **cross-site** → la cookie `lax` NO viaja y el login no
> funciona, aunque CORS esté bien. Haría falta `COOKIE_SAMESITE=none`, que afloja la política para
> todos los usuarios de producción. Por eso la migración se hizo con cutover directo sobre `www`,
> sin staging en `pages.dev`.

- **Render deploya desde `main`, NO desde `dev`.** `dev` = staging; se promueve con el workflow
  **manual** de GitHub Actions **"Promote dev to main"** (Actions → Run workflow): corre CI sobre
  `dev` (compila + importa la app) y si pasa mergea `dev`→`main` y pushea (dispara el CD de
  Render). **No promover a mano** (un commit local se cuela a main). `promote.yml` vive **solo en
  `main`** (rama default) para que aparezca el botón.
- **Cloudflare WAF (plan Free)** — reglas activas (2026-07-24):
  - Custom rule 1 (Skip): `/.well-known/` + `/payments/webhook` → saltea todo el WAF (protege la
    renovación del cert y el webhook de MP).
  - Custom rule "Admin solo Argentina" (Block): `starts_with "/admin"` o `/ingresar` con `ip.src.country ne "AR"` (ampliada 2026-08-03).
  - Custom rule "Challenge fuera de LATAM" (Managed Challenge): acotada a `http.host eq
    "www.corpolab3d.com"` (NO api, o rompería los fetch del front con challenge) y `not cf.client.bot`.
  - Rate limiting rule (0/1 del Free): `/auth/login` POST, 5/10s → Block (borde).
  - Transform Rule: inyecta `x-origin-secret` en `api.corpolab3d.com` (el guard de origen).
  - Cupos: custom 3/5 · rate-limiting 1/1 · transform 1/10.
  - **Transform rules tras el alta de QA (2026-08-14): 3/10.** Se sumaron `Inyectar secreto de
    origen QA` (request, `api-qa.corpolab3d.com`) y `noindex QA` (**response**,
    `qa.corpolab3d.com` → `X-Robots-Tag`). Las custom rules **no** se tocaron, así que las que
    filtran por path (`/admin`, `/ingresar`, el Skip de `/payments/webhook`) **también aplican a
    los hostnames de QA**; la de "Challenge fuera de LATAM" no, porque está acotada a
    `www.corpolab3d.com`.

## Flujo de ramas (aplica igual que en el front)

**`qa` es TESTING, `dev` es STAGING.** `qa` es una rama **independiente**: no es un espejo de
`dev` y nunca se resetea contra ella.

**`feature/*` → `qa` → (Leo aprueba) → `dev` → `main`.** Se mergea la rama de feature a `qa`
para probarla en `api-qa.corpolab3d.com`; **el merge a `dev` lo pide Leo explícitamente** y
lleva la misma rama de feature, nunca `qa`.

- **Prohibido** `git push origin origin/dev:qa --force` salvo pedido explícito de Leo.
- **`dev` no se toca sin pedido explícito**, aunque el testing en `qa` haya salido bien.
- Cuidado extra en este repo: **`dev` no deploya nada, pero `qa` y `main` sí**, y `start.sh`
  corre `alembic upgrade head` al arrancar. O sea que **pushear a `qa` aplica las migraciones
  pendientes contra la DB Neon de QA** (y pushear a `main`, contra la de producción). No es un
  merge inocuo: si la migración es destructiva, el rollback es restaurar la DB.

Regla completa, con el porqué y el incidente que la originó, en el `CLAUDE.md` de la raíz →
"Flujo de ramas (Git)". **Es la única fuente de verdad**: si esta sección y esa difieren, manda
la de la raíz.

## Entorno de QA (2026-08-14)

Servicio Render **`backend-corpolab3d-qa`** (plan **Free**, Ohio, deploya desde la rama **`qa`**),
sirviendo `https://api-qa.corpolab3d.com` contra una DB Neon propia (`corpolab3d-qa`, Ohio).
La topología completa —front, riesgos aceptados, cómo crear un admin— vive en el `CLAUDE.md` de
la raíz → "Entorno de QA". Acá va solo lo que hace al backend.

- **Creado A MANO en el dashboard, NO por `render.yaml`.** Ese blueprint declara
  `name: backend-corpolab3d` y `plan: starter`: aplicarlo reconciliaría el servicio de
  **producción**, no crearía uno nuevo. Mismo criterio que los crons desde 2026-08-04.
- **Env vars propias**: `DATABASE_URL` (Neon QA), `SESSION_SECRET` (propio),
  `CORS_ORIGINS=["https://qa.corpolab3d.com"]`, `FRONTEND_URL=https://qa.corpolab3d.com`,
  `BACKEND_URL=https://api-qa.corpolab3d.com`, `ORIGIN_SECRET` (hex propio),
  `REDIS_URL=...:6379/1`, `R2_*` (bucket y token propios), `RESEND_API_KEY` (key propia) y
  `EMAIL_FROM=CorpoLab 3D QA <no-reply@corpolab3d.com>`. **`MP_*` sin cargar** → `/payments/*`
  responde 503 en QA (ver TODO de la raíz).
- **`ENVIRONMENT=production` también acá**, para que la cookie salga con `Secure` igual que en
  producción. Ver el porqué en el `CLAUDE.md` de la raíz.
- **El plan Free no tiene shell.** `scripts/create_admin.py` y cualquier script puntual se corren
  **locales contra Neon QA** con la imagen de compose (`docker run --env-file ...`). Los crons
  **no** existen en QA: si hiciera falta probar uno, mismo camino.
- **Free duerme a los 15 min de inactividad**: el primer request paga ~40-50 s de arranque en
  frío, encima del autosuspend de Neon. Cloudflare corta a los 100 s con **524**, así que un 524
  aislado en el primer hit del día no es un bug del backend.
- **El guard de origen funciona igual que en prod, con secreto propio.** Verificado el
  2026-08-14: `api-qa.corpolab3d.com/auth/me` → **401**,
  `backend-corpolab3d-qa.onrender.com/auth/me` → **403**.
- **Sin `RESEND_API_KEY`, los 13 `send_*` loguean el link/código en vez de enviar** (el guard
  mira solo la key, no `ENVIRONMENT`). Es una red de contención útil mientras se prueba lógica:
  imposible mandarle un mail a un cliente por un typo. Al cargar la key esa red desaparece.

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
| Resend | TXT (DMARC) | `_dmarc` | `v=DMARC1; p=reject;` |

> **No pisar el SPF de la raiz** (Email Routing): el SPF de Resend vive en el subdominio `send`,
> no en la raiz. Con el dominio verificado en Resend ya **no hay bloqueo tecnico para activar el
> OTP de login** (`OTP_ENABLED`); esa activacion queda como decision aparte. DMARC en
> `p=reject` (endurecido a reject 2026-08-06; antes quarantine desde 2026-08-03). El WAF no interviene (el mail va por MX/SMTP, no HTTP).

**Estado (2026-07-24):** recepcion (info/support/soporte/contacto/ventas + catch-all APAGADO →
las inexistentes rebotan) **probada OK**. Dominio en Resend **verificado**. `EMAIL_FROM` en Render
= `no-reply@corpolab3d.com`. **Envio real VERIFICADO**: un `forgot-password` de prod llego
`From: no-reply@corpolab3d.com` con **SPF+DKIM+DMARC = PASS**. Frontend actualizado: landing y
paginas legales apuntan a `contacto@corpolab3d.com` (rama `fix/contacto-emails` mergeada a `dev`).
**Pendiente** (ver "TODO / pendiente"): Gmail "Enviar como" — hecho para `info@` y `contacto@`
(2026-08-10), falta `support@`, `soporte@` y `ventas@`. La API key `gmail-smtp` de Resend esta
en Bitwarden. Los
templates branded de los mails (header/footer + tema claro, Jinja2 en `app/mailing/`) ya están
**HECHOS** (2026-07-24); verificación visual en Gmail/Outlook **OK** (2026-08-03).

## Convenciones / cuidados

- **Respuestas al usuario (chat)**: español SIN tildes/acentos (ej. "Confirmas"). Solo el chat,
  no el código ni estos docs.
- Lógica de validación de sesión: **una sola** (`get_current_user`); no duplicarla por endpoint.
- Cambios de schema → nueva migración Alembic en `alembic/versions/`, no editar la `0001`.
- `psycopg[binary]` en local (Windows/glibc) y en el contenedor (hay wheels musllinux; trae
  `libpq` embebida — psycopg puro falla en Alpine por `ctypes.find_library`).

## TODO / pendiente

> **Los TODO viven todos en el `CLAUDE.md` de la raíz** (`../CLAUDE.md` → "TODO / pendiente
> (repo)"), unificados con los del front. No agregar pendientes acá: se duplican y se
> desincronizan. Lo que sí va en este archivo es **cómo funciona** el backend; el qué falta,
> arriba.

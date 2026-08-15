import logging
from datetime import datetime, timedelta, timezone

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, require_admin, require_captcha
from app.email import (
    send_login_otp_email,
    send_password_reset_email,
    send_signup_verification_email,
    send_welcome_email,
)
from app.google_oauth import GoogleAuthError, verify_google_id_token
from app.models import (
    PasswordResetToken,
    PendingRegistration,
    Session,
    Subscription,
    User,
)
from app.otp import RedisUnavailableError, has_active_otp
from app.otp import store_otp as redis_store_otp
from app.otp import verify_otp as redis_verify_otp
from app.ratelimit import limiter
# `payments` no importa `auth`, asi que no hay ciclo.
from app.routers.payments import cancel_preapproval
from app.routers.tiers import sync_user_tier
from app.schemas import (
    ChangePasswordIn,
    ForgotPasswordIn,
    GoogleAuthIn,
    LoginIn,
    LoginResponse,
    RegisterIn,
    ResendOtpIn,
    ResetPasswordIn,
    AcceptLegalIn,
    SignupIn,
    UserOut,
    VerifyOtpIn,
    VerifySignupIn,
)
from app.security import (
    generate_otp,
    generate_session_token,
    generate_token,
    hash_password,
    hash_session_token,
    hash_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

logger = logging.getLogger("auth")


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.session_days * 24 * 60 * 60,  # 7 días
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path="/",
    )


def _start_session(db: DbSession, user: User, response: Response) -> None:
    """Cierra el login: crea la sesion (token plano solo en la cookie, en DB solo su
    HMAC), reconcilia el tier y setea la cookie. Igual que el final del login viejo;
    ahora lo usa `verify_otp` (el 2do paso) y queda disponible para reuso."""
    token = generate_session_token()
    db.add(
        Session(
            user_id=user.id,
            token_hash=hash_session_token(token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.session_days),
        )
    )
    db.commit()
    # Reconcilia el tier una vez por login: si su tier pago vencio, lo degrada a
    # free (el login solo degrada; "pagar" es accion del admin via PUT /tiers).
    sync_user_tier(db, user, datetime.now(timezone.utc))
    _set_session_cookie(response, token)


def _issue_login_otp(user: User, background: BackgroundTasks) -> None:
    """Emite un OTP de login via Redis: invalida el anterior (DEL), guarda el HMAC
    del nuevo con TTL nativo y manda el codigo por email en background."""
    code = generate_otp()
    redis_store_otp(user.id, hash_token(code))
    background.add_task(send_login_otp_email, user.email, code)


def _otp_required(user: User) -> bool:
    """Los admins pasan SIEMPRE por el 2do factor, tengan el switch global prendido o
    no: son las cuentas que crean usuarios, mueven tiers y ven pagos. Para el resto
    manda `OTP_ENABLED`."""
    return settings.otp_enabled or user.is_admin


@router.post(
    "/login",
    response_model=LoginResponse,
    dependencies=[Depends(require_captcha("login"))],
)
@limiter.limit("5/minute")
def login(
    request: Request,
    payload: LoginIn,
    background: BackgroundTasks,
    response: Response,
    db: DbSession = Depends(get_db),
):
    """Paso 1 del login. Valida credenciales y, segun `_otp_required`:
    - ON:  dispara un OTP por email y responde `otp_required=True` (sin sesion aun;
      el cliente verifica el codigo en `/auth/verify-otp`). Se activa con
      `OTP_ENABLED=true` para todos, o siempre que el usuario sea admin.
    - OFF: inicia sesion directo (cookie) y responde `otp_required=False` + el user,
      igual que el login de un solo paso de antes."""
    user = db.scalar(select(User).where(User.email == payload.email))
    # Verificar siempre el password (aunque el user no exista) para no filtrar
    # por timing si un email está registrado.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Credenciales invalidas")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Usuario inactivo")

    if not _otp_required(user):
        _start_session(db, user, response)
        return LoginResponse(otp_required=False, user=user)

    try:
        _issue_login_otp(user, background)
    except RedisUnavailableError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Servicio temporalmente no disponible")
    return LoginResponse(otp_required=True)


@router.post(
    "/verify-otp",
    response_model=UserOut,
    dependencies=[Depends(require_captcha("verify_otp"))],
)
@limiter.limit("10/minute")
def verify_otp(
    request: Request,
    payload: VerifyOtpIn,
    response: Response,
    db: DbSession = Depends(get_db),
):
    """Paso 2 del login: consume el OTP (Redis) y, si es valido, inicia la sesion
    (cookie en Postgres). Respuesta 400 GENERICA (anti-enumeracion)."""
    invalid = HTTPException(status.HTTP_400_BAD_REQUEST, detail="Codigo invalido o expirado")

    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not user.is_active:
        raise invalid

    try:
        ok = redis_verify_otp(user.id, hash_token(payload.code))
    except RedisUnavailableError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Servicio temporalmente no disponible")
    if not ok:
        raise invalid

    _start_session(db, user, response)
    return user


@router.post(
    "/resend-otp",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_captcha("resend_otp"))],
)
@limiter.limit("1/minute")
def resend_otp(
    request: Request,
    payload: ResendOtpIn,
    background: BackgroundTasks,
    db: DbSession = Depends(get_db),
):
    """Reenvia un OTP nuevo (boton "reenviar" de la pantalla de verificacion).
    Responde SIEMPRE 204, exista o no el email (anti-enumeracion).

    Cooldown server-side: NO reenvia si el usuario todavia tiene un codigo ACTIVO
    en Redis (la key existe = codigo vivo). Al expirar el TTL, el reenvio vuelve a
    estar disponible."""
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not user.is_active:
        return
    # Solo emite para quien realmente pasa por OTP: si no, este endpoint seria un
    # "mandale un mail a esta direccion" sin autenticacion (con OTP_ENABLED=false,
    # un no-admin nunca llega a la pantalla de verificacion).
    if not _otp_required(user):
        return
    try:
        if has_active_otp(user.id):
            return
        _issue_login_otp(user, background)
    except RedisUnavailableError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Servicio temporalmente no disponible")


@router.post(
    "/google",
    response_model=UserOut,
    dependencies=[Depends(require_captcha("google_login"))],
)
@limiter.limit("10/minute")
def google_login(
    request: Request,
    payload: GoogleAuthIn,
    response: Response,
    db: DbSession = Depends(get_db),
):
    """Login con Google (OIDC). Google SOLO verifica identidad: el usuario vive en
    nuestra tabla `users` y emitimos nuestra propia cookie de sesion (identico al
    final de `login`, el front no nota diferencia). Autocrea (tier free) si el email
    no existe, o linkea el `google_sub` a una cuenta password existente del mismo
    email. La fuente de verdad es el ID token verificado en el server, nunca el
    `credential` crudo del navegador."""
    try:
        info = verify_google_id_token(payload.credential)
    except GoogleAuthError:
        # Token invalido/expirado/aud incorrecta, o Google sin configurar.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Credencial de Google invalida")

    # email_verified obligatorio: solo asi es seguro crear/linkear por email.
    if not info.get("email_verified"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Email de Google no verificado")

    sub = info["sub"]
    email = info["email"]
    name = info.get("name") or email.split("@")[0]  # la app usa full_name para mostrar

    # 1ro por google_sub (id estable); si no, por email (para linkear cuentas password).
    user = db.scalar(select(User).where(User.google_sub == sub))
    if user is None:
        user = db.scalar(select(User).where(User.email == email))
        if user is not None:
            # Linkeo: conserva su password_hash (puede seguir usando ambos metodos).
            user.google_sub = sub
            if not user.full_name:
                user.full_name = name
        else:
            # Autocreacion: usuario comun, sin password, tier free (sin fila lazy).
            user = User(
                email=email,
                full_name=name,
                password_hash=None,
                is_admin=False,
                is_active=True,
                google_sub=sub,
                auth_provider="google",
            )
            db.add(user)

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Usuario inactivo")

    db.flush()  # asigna user.id si recien se creo (para la FK de Session)

    # Auto-login: misma cola de sesion que `login`.
    token = generate_session_token()
    db.add(
        Session(
            user_id=user.id,
            token_hash=hash_session_token(token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.session_days),
        )
    )
    db.commit()

    # Reconcilia el tier (degrada si vencio); no-op para un usuario recien creado.
    sync_user_tier(db, user, datetime.now(timezone.utc))

    _set_session_cookie(response, token)
    return user


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: DbSession = Depends(get_db)):
    token = request.cookies.get(settings.cookie_name)
    if token:
        session = db.scalar(
            select(Session).where(Session.token_hash == hash_session_token(token))
        )
        if session and session.revoked_at is None:
            session.revoked_at = datetime.now(timezone.utc)
            db.commit()
    # delete_cookie debe matchear path/samesite/secure/domain para que el navegador la borre
    response.delete_cookie(
        key=settings.cookie_name,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
    )


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],  # solo un admin autenticado puede crear usuarios
)
@limiter.limit("10/minute")
def register(request: Request, payload: RegisterIn, db: DbSession = Depends(get_db)):
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email ya registrado")
    user = User(
        email=payload.email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post(
    "/signup",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_captcha("signup"))],
)
@limiter.limit("5/minute")
def signup(
    request: Request,
    payload: SignupIn,
    background: BackgroundTasks,
    db: DbSession = Depends(get_db),
):
    """Alta self-serve (PUBLICA, sin admin) con verificacion de email (double
    opt-in). NO crea el usuario ni inicia sesion: guarda un `PendingRegistration`
    (email + full_name + password ya hasheado + token) y manda un link por email.
    La cuenta se crea RECIEN al consumir el token en `/auth/verify-signup`; ahi el
    usuario tiene que ingresar de nuevo (no hay auto-login).

    - Si el email ya tiene cuenta CONFIRMADA -> 409 (mismo comportamiento de antes).
    - Si hay un pendiente sin confirmar -> se invalida y se emite uno nuevo (un solo
      link vivo a la vez), asi se puede reintentar si el primer mail no llego."""
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email ya registrado")

    # Invalida los pendientes previos sin usar de este email (un link vivo a la vez).
    db.execute(
        update(PendingRegistration)
        .where(
            PendingRegistration.email == payload.email,
            PendingRegistration.used_at.is_(None),
        )
        .values(used_at=datetime.now(timezone.utc))
    )
    token = generate_token()
    db.add(
        PendingRegistration(
            email=payload.email,
            full_name=payload.full_name,
            password_hash=hash_password(payload.password),
            token_hash=hash_token(token),
            expires_at=datetime.now(timezone.utc)
            + timedelta(minutes=settings.signup_token_minutes),
            # Aceptacion de los legales: los bools ya los exigio SignupIn (422 si
            # alguno no viene True). Las VERSIONES las pone el servidor, no el
            # cliente. Se sellan las dos: el alta nueva nunca pasa por /politicas.
            terms_accepted_at=datetime.now(timezone.utc),
            terms_version=settings.terms_version,
            privacy_accepted_at=datetime.now(timezone.utc),
            privacy_version=settings.privacy_version,
        )
    )
    db.commit()
    link = f"{settings.frontend_url.rstrip('/')}/confirmar-registro?token={token}"
    # Envio en background: la respuesta no espera al proveedor.
    background.add_task(send_signup_verification_email, payload.email, link)


@router.post(
    "/verify-signup",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_captcha("verify_signup"))],
)
@limiter.limit("10/minute")
def verify_signup(
    request: Request,
    payload: VerifySignupIn,
    background: BackgroundTasks,
    db: DbSession = Depends(get_db),
):
    """Confirma el alta (2do paso del double opt-in): consume el token del email y
    crea el usuario real. Token single-use + corto. NO inicia sesion: el usuario
    tiene que ingresar despues (igual que /reset-password)."""
    invalid = HTTPException(status.HTTP_400_BAD_REQUEST, detail="Token invalido o expirado")

    pending = db.scalar(
        select(PendingRegistration).where(
            PendingRegistration.token_hash == hash_token(payload.token)
        )
    )
    if (
        pending is None
        or pending.used_at is not None
        or pending.expires_at <= datetime.now(timezone.utc)
    ):
        raise invalid

    # Consumimos el token pase lo que pase (single-use).
    pending.used_at = datetime.now(timezone.utc)

    # Guard de carrera / doble click: si la cuenta ya existe, no duplicamos; devolvemos
    # el usuario existente (idempotente).
    user = db.scalar(select(User).where(User.email == pending.email))
    is_new = user is None
    if is_new:
        user = User(
            email=pending.email,
            full_name=pending.full_name,
            # Ya viene hasheado del signup: NO re-hashear.
            password_hash=pending.password_hash,
            is_admin=False,  # forzado: el alta publica nunca crea admins
            # Se copia el momento del SIGNUP (cuando tildo los checkboxes), no el
            # de ahora: lo que vale como aceptacion es el clic, no la confirmacion.
            terms_accepted_at=pending.terms_accepted_at,
            terms_version=pending.terms_version,
            privacy_accepted_at=pending.privacy_accepted_at,
            privacy_version=pending.privacy_version,
        )
        db.add(user)

    db.commit()
    db.refresh(user)
    # Bienvenida solo en el alta real (no en el caso idempotente de doble click), y
    # despues del commit para no mandarla si la transaccion falla. En BackgroundTask.
    if is_new:
        background.add_task(send_welcome_email, user.email, user.full_name)
    return user


@router.post("/accept-legal", response_model=UserOut)
@limiter.limit("10/minute")
def accept_legal(
    request: Request,
    payload: AcceptLegalIn,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    """Acepta los legales desde /politicas: es la via para los usuarios que ya
    tenian cuenta cuando el checkbox no existia, para las altas hechas por admin, y
    para cuando sube la version de un documento.

    Usa `get_current_user` y NO `require_legal_acceptance`, obviamente: exigir la
    aceptacion para poder aceptar seria un deadlock.

    Sella la version VIGENTE del servidor, no una que mande el cliente. Solo toca
    los documentos que vienen en `true`: aceptar uno solo deja el otro pendiente (el
    usuario sigue bloqueado, que es lo correcto) en vez de sellar los dos de prepo.
    Idempotente: re-aceptar solo refresca la fecha."""
    now = datetime.now(timezone.utc)
    if payload.accept_terms:
        user.terms_accepted_at = now
        user.terms_version = settings.terms_version
    if payload.accept_privacy:
        user.privacy_accepted_at = now
        user.privacy_version = settings.privacy_version
    db.commit()
    db.refresh(user)
    # Devuelve el UserOut ya actualizado para que el front refresque su store sin
    # una segunda llamada a /auth/me (y no quede el middleware rebotando con los
    # flags viejos, el mismo pozo que ya tiene documentado must_change_password).
    return user


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/minute")
def change_password(
    request: Request,
    payload: ChangePasswordIn,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    """Cambio de la propia contraseña: requiere sesion activa (get_current_user) y
    verifica la clave actual. A diferencia de /reset-password, NO revoca sesiones:
    la sesion actual (y las demas) siguen vivas."""
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Contrasena actual incorrecta")
    user.password_hash = hash_password(payload.new_password)
    # La clave ya es del usuario, no la temporal que le puso un admin: se libera el
    # bloqueo del front. Este es el camino normal para salir de must_change_password.
    user.must_change_password = False
    db.commit()


@router.post("/deactivate", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("3/minute")
def deactivate_account(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    """Baja de la propia cuenta (self-service). BORRADO BLANDO, no `DELETE`.

    Se conserva la fila porque `payments` y `export_logs` la referencian: un hard
    delete los deja con `user_id=NULL` y se pierde a quien correspondia cada cobro,
    que es justo lo que la normativa fiscal obliga a conservar. `is_active=False`
    corta el acceso igual de rapido —`deps.py` lo valida en CADA request— y
    `deactivated_at` deja la constancia de cuando entro el pedido.

    Usa `get_current_user` y NO `require_legal_acceptance`, igual que `/logout` y
    `/accept-legal`: obligar a aceptar un contrato para poder irse seria coercitivo.
    Un usuario trabado en /politicas tiene que poder darse de baja.

    OJO: `is_active=False` no toca la preapproval, asi que sin cancelarla MP le
    seguiria cobrando a alguien que ya se fue. La cancelacion es best-effort: si MP
    no responde, la baja se persiste igual (revertirla por un fallo de un tercero
    seria peor) y queda el log para resolverlo a mano.
    """
    sub = db.scalar(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.mp_status == "authorized",
        )
    )
    if sub is not None and not cancel_preapproval(sub):
        logger.error(
            "Baja de cuenta %s: no se pudo cancelar la suscripcion %s en MP. "
            "CANCELARLA A MANO en el panel de Mercado Pago.",
            user.id, sub.mp_preapproval_id,
        )

    now = datetime.now(timezone.utc)
    user.is_active = False
    user.deactivated_at = now
    # La sesion actual moriria igual en el proximo request por el chequeo de
    # `is_active` en deps.py, pero se revocan todas explicitamente: deja el motivo
    # asentado en la tabla y no depende de que ese chequeo siga estando.
    db.execute(
        update(Session)
        .where(Session.user_id == user.id, Session.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.commit()

    response.delete_cookie(
        key=settings.cookie_name,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
    )


@router.post(
    "/forgot-password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_captcha("forgot_password"))],
)
@limiter.limit("3/minute")
def forgot_password(
    request: Request,
    payload: ForgotPasswordIn,
    background: BackgroundTasks,
    db: DbSession = Depends(get_db),
):
    """Pide un link de reset. Responde SIEMPRE 204, exista o no el email, para no
    filtrar que cuentas estan registradas (anti-enumeracion)."""
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is not None and user.is_active:
        # Invalida tokens previos sin usar de este usuario (un link vivo a la vez).
        db.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.used_at.is_(None),
            )
            .values(used_at=datetime.now(timezone.utc))
        )
        token = generate_token()
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=datetime.now(timezone.utc)
                + timedelta(minutes=settings.reset_token_minutes),
            )
        )
        db.commit()
        link = f"{settings.frontend_url.rstrip('/')}/reset-password?token={token}"
        # Envio en background: la respuesta no espera al proveedor (ni filtra timing).
        background.add_task(send_password_reset_email, user.email, link)


@router.post(
    "/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_captcha("reset_password"))],
)
@limiter.limit("5/minute")
def reset_password(request: Request, payload: ResetPasswordIn, db: DbSession = Depends(get_db)):
    """Consume el token y setea la nueva contraseña. Token single-use + corto.
    Al resetear, revoca TODAS las sesiones activas del usuario (re-login forzado)."""
    reset = db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_token(payload.token)
        )
    )
    if (
        reset is None
        or reset.used_at is not None
        or reset.expires_at <= datetime.now(timezone.utc)
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Token invalido o expirado")

    user = db.get(User, reset.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Token invalido o expirado")

    user.password_hash = hash_password(payload.password)
    # Igual que en change-password: la clave la eligio el usuario, asi que deja de
    # ser temporal. Sin esto, alguien que recibio una clave del panel y despues uso
    # "olvide mi contraseña" quedaria trabado en /cambiar-password para siempre.
    user.must_change_password = False
    reset.used_at = datetime.now(timezone.utc)
    # Revocar todas las sesiones activas: un reset cierra la sesion en todos lados.
    db.execute(
        update(Session)
        .where(Session.user_id == user.id, Session.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    db.commit()

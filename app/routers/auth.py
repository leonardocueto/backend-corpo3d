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
from app.deps import get_current_user, require_admin
from app.email import send_login_otp_email, send_password_reset_email
from app.google_oauth import GoogleAuthError, verify_google_id_token
from app.models import LoginOtp, PasswordResetToken, Session, User
from app.ratelimit import limiter
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
    SignupIn,
    UserOut,
    VerifyOtpIn,
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


def _issue_login_otp(db: DbSession, user: User, background: BackgroundTasks) -> None:
    """Emite un OTP de login: invalida los OTP previos sin usar del usuario (un solo
    codigo activo a la vez), guarda el HMAC del nuevo con vida corta y manda el codigo
    por email en background (la respuesta no espera al proveedor)."""
    db.execute(
        update(LoginOtp)
        .where(LoginOtp.user_id == user.id, LoginOtp.used_at.is_(None))
        .values(used_at=datetime.now(timezone.utc))
    )
    code = generate_otp()
    db.add(
        LoginOtp(
            user_id=user.id,
            code_hash=hash_token(code),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.otp_minutes),
        )
    )
    db.commit()
    background.add_task(send_login_otp_email, user.email, code)


@router.post("/login", response_model=LoginResponse)
@limiter.limit("5/minute")
def login(
    request: Request,
    payload: LoginIn,
    background: BackgroundTasks,
    response: Response,
    db: DbSession = Depends(get_db),
):
    """Paso 1 del login. Valida credenciales y, segun `OTP_ENABLED`:
    - ON:  dispara un OTP por email y responde `otp_required=True` (sin sesion aun;
      el cliente verifica el codigo en `/auth/verify-otp`).
    - OFF: inicia sesion directo (cookie) y responde `otp_required=False` + el user,
      igual que el login de un solo paso de antes."""
    user = db.scalar(select(User).where(User.email == payload.email))
    # Verificar siempre el password (aunque el user no exista) para no filtrar
    # por timing si un email está registrado.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Credenciales invalidas")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Usuario inactivo")

    if not settings.otp_enabled:
        # Sin OTP: login directo (compat con el flujo previo).
        _start_session(db, user, response)
        return LoginResponse(otp_required=False, user=user)

    _issue_login_otp(db, user, background)
    return LoginResponse(otp_required=True)


@router.post("/verify-otp", response_model=UserOut)
@limiter.limit("10/minute")
def verify_otp(
    request: Request,
    payload: VerifyOtpIn,
    response: Response,
    db: DbSession = Depends(get_db),
):
    """Paso 2 del login: consume el OTP y, si es valido, inicia la sesion (cookie).
    Codigo single-use + corto + con tope de intentos. Respuesta 400 GENERICA: no
    distingue "no existe" de "vencido" de "incorrecto" (anti-enumeracion)."""
    invalid = HTTPException(status.HTTP_400_BAD_REQUEST, detail="Codigo invalido o expirado")

    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not user.is_active:
        raise invalid

    # OTP mas reciente sin usar del usuario (un solo activo a la vez).
    otp = db.scalar(
        select(LoginOtp)
        .where(LoginOtp.user_id == user.id, LoginOtp.used_at.is_(None))
        .order_by(LoginOtp.created_at.desc())
    )
    now = datetime.now(timezone.utc)
    if otp is None or otp.expires_at <= now or otp.attempts >= settings.otp_max_attempts:
        raise invalid

    if otp.code_hash != hash_token(payload.code):
        # Cuenta el intento fallido; al llegar al tope, el codigo queda inservible.
        otp.attempts += 1
        if otp.attempts >= settings.otp_max_attempts:
            otp.used_at = now  # invalida explicitamente tras agotar los intentos
        db.commit()
        raise invalid

    otp.used_at = now
    _start_session(db, user, response)
    return user


@router.post("/resend-otp", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("1/minute")
def resend_otp(
    request: Request,
    payload: ResendOtpIn,
    background: BackgroundTasks,
    db: DbSession = Depends(get_db),
):
    """Reenvia un OTP nuevo (boton "reenviar" de la pantalla de verificacion).
    Responde SIEMPRE 204, exista o no el email (anti-enumeracion)."""
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is not None and user.is_active:
        _issue_login_otp(db, user, background)


@router.post("/google", response_model=UserOut)
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


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
def signup(
    request: Request,
    payload: SignupIn,
    response: Response,
    db: DbSession = Depends(get_db),
):
    """Alta self-serve (PUBLICA, sin admin). Crea SIEMPRE un usuario comun
    (`is_admin=False`) en tier free (sin fila en `user_tiers`: free es lazy) e
    inicia sesion al toque (cookie HttpOnly), igual que `login`. El front recibe
    el user y la cookie en una sola llamada."""
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email ya registrado")
    user = User(
        email=payload.email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        is_admin=False,  # forzado: el endpoint publico nunca crea admins
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Auto-login: misma cola de sesion que `login` (token plano solo en la cookie,
    # en DB solo su HMAC). Un usuario nuevo no tiene tier que sincronizar.
    token = generate_session_token()
    db.add(
        Session(
            user_id=user.id,
            token_hash=hash_session_token(token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.session_days),
        )
    )
    db.commit()

    _set_session_cookie(response, token)
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
    db.commit()


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
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


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
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
    reset.used_at = datetime.now(timezone.utc)
    # Revocar todas las sesiones activas: un reset cierra la sesion en todos lados.
    db.execute(
        update(Session)
        .where(Session.user_id == user.id, Session.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    db.commit()

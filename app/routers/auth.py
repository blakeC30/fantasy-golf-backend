"""
Auth router — /auth/*

Endpoints:
  POST /auth/register         Create a new account with email + password
  POST /auth/login            Exchange credentials for JWT tokens
  POST /auth/google           Exchange a Google ID token for JWT tokens
  POST /auth/refresh          Use the refresh cookie to get a new access token
  POST /auth/logout           Clear the refresh token cookie
  POST /auth/forgot-password  Request a password reset email
  POST /auth/reset-password   Submit a new password using a reset token
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from google.auth.exceptions import GoogleAuthError
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

from app.config import settings
from app.limiter import limiter
from app.database import get_db
from app.dependencies import get_current_user, get_refresh_token_user
from app.models import User
from app.schemas.auth import (
    ForgotPasswordRequest,
    GoogleAuthRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
)
from app.schemas.user import UserOut
from app.services.auth import (
    consume_reset_token,
    create_access_token,
    create_refresh_token,
    generate_reset_token,
    hash_password,
    validate_reset_token,
    verify_google_id_token,
    verify_password,
)
from app.services.email import send_password_reset_email

router = APIRouter(prefix="/auth", tags=["auth"])

_REFRESH_COOKIE = "refresh_token"
_REFRESH_MAX_AGE = 7 * 24 * 60 * 60  # 7 days in seconds


def _set_refresh_cookie(response: Response, token: str) -> None:
    """Attach the refresh token as a secure httpOnly cookie."""
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=token,
        httponly=True,                                    # JS cannot read it
        secure=settings.ENVIRONMENT == "production",     # HTTPS only in prod
        samesite="lax",                                   # CSRF protection
        max_age=_REFRESH_MAX_AGE,
    )


def _issue_tokens(user: User, response: Response) -> TokenResponse:
    """Create access + refresh tokens for a user and attach the refresh cookie."""
    access = create_access_token(str(user.id))
    refresh = create_refresh_token(str(user.id))
    _set_refresh_cookie(response, refresh)
    return TokenResponse(access_token=access)


@router.post("/register", response_model=TokenResponse, status_code=201)
@limiter.limit("5/hour")
def register(request: Request, body: RegisterRequest, response: Response, db: Session = Depends(get_db)):
    """
    Create a new user account.

    Returns an access token immediately so the user is logged in right after
    registration — no separate login step required.
    """
    if db.query(User).filter_by(email=body.email.lower()).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    user = User(
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return _issue_tokens(user, response)


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
def login(request: Request, body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    """Exchange email + password for JWT tokens."""
    user = db.query(User).filter_by(email=body.email.lower()).first()

    # Check both cases with the same error to prevent email enumeration.
    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return _issue_tokens(user, response)


@router.post("/google", response_model=TokenResponse)
@limiter.limit("10/minute")
def google_auth(request: Request, body: GoogleAuthRequest, response: Response, db: Session = Depends(get_db)):
    """
    Authenticate via Google Sign-In.

    The frontend sends the Google-issued ID token; we verify it server-side
    using the google-auth library. No secret is ever sent from the browser.

    If the Google account matches an existing user (by google_id or email),
    we log them in. Otherwise we create a new account.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=501, detail="Google authentication is not configured")

    try:
        claims = verify_google_id_token(body.id_token)
    except GoogleAuthError as exc:
        # Token is invalid, expired, or issued for a different client — return 401.
        log.warning("Google token verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid Google ID token")
    except Exception:
        # Unexpected error (misconfigured GOOGLE_CLIENT_ID, network failure, library
        # bug). Re-raise so FastAPI returns 500 and the traceback appears in logs —
        # swallowing it as a 401 would hide configuration problems silently.
        log.exception("Unexpected error during Google token verification")
        raise

    google_id = claims["sub"]
    email = claims.get("email", "").lower()
    name = claims.get("name", email)

    # Try to find the user by google_id first, then by email (account linking).
    user = db.query(User).filter_by(google_id=google_id).first()
    if not user and email:
        user = db.query(User).filter_by(email=email).first()
        if user:
            # Link the Google account to the existing email account.
            user.google_id = google_id
            db.commit()

    if not user:
        # First-time Google sign-in: create a new account.
        user = User(email=email, google_id=google_id, display_name=name)
        db.add(user)
        db.commit()
        db.refresh(user)

    return _issue_tokens(user, response)


@router.post("/refresh", response_model=TokenResponse)
def refresh_token(
    response: Response,
    user: User = Depends(get_refresh_token_user),
):
    """
    Issue a new access token using the refresh token cookie.

    The refresh token is read from the httpOnly cookie — the client never
    passes it explicitly. This endpoint is called automatically by the
    frontend's axios interceptor when a 401 is received.
    """
    access = create_access_token(str(user.id))
    return TokenResponse(access_token=access)


@router.post("/forgot-password", status_code=200)
@limiter.limit("3/hour")
def forgot_password(request: Request, body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """
    Send a password reset email.

    Always returns 200 regardless of whether the email exists in the system,
    to prevent email enumeration attacks. Google-only accounts (no password_hash)
    are silently skipped — they cannot reset a password they never set.
    """
    user = db.query(User).filter_by(email=body.email.lower()).first()
    if user and user.password_hash:
        raw = generate_reset_token(db, user)
        try:
            send_password_reset_email(user.email, raw)
        except Exception:
            # Log the error but don't let a SES failure expose account existence.
            log.exception("Failed to send password reset email to %s", user.email)
    return {"detail": "If an account with that email exists, a reset link has been sent."}


@router.post("/reset-password", response_model=TokenResponse)
@limiter.limit("10/hour")
def reset_password(
    request: Request,
    body: ResetPasswordRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    """
    Set a new password using a reset token from the email link.

    The token must be valid, unused, and not expired (1-hour TTL). On success
    the token is consumed and the user is logged in immediately (access + refresh
    tokens are issued), so they don't need a separate login step.
    """
    user = validate_reset_token(db, body.token)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link.")
    user.password_hash = hash_password(body.new_password)
    consume_reset_token(db, body.token)
    db.commit()
    return _issue_tokens(user, response)


@router.post("/logout", status_code=204)
def logout(response: Response):
    """
    Clear the refresh token cookie.

    The client is responsible for discarding the access token from memory.
    Since access tokens are short-lived (15 min), they expire on their own.
    """
    response.delete_cookie(key=_REFRESH_COOKIE)


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    """Return the currently authenticated user."""
    return current_user

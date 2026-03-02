"""
Auth router — /auth/*

Endpoints:
  POST /auth/register   Create a new account with email + password
  POST /auth/login      Exchange credentials for JWT tokens
  POST /auth/google     Exchange a Google ID token for JWT tokens
  POST /auth/refresh    Use the refresh cookie to get a new access token
  POST /auth/logout     Clear the refresh token cookie
"""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user, get_refresh_token_user
from app.models import User
from app.schemas.auth import GoogleAuthRequest, LoginRequest, RegisterRequest, TokenResponse
from app.schemas.user import UserOut
from app.services.auth import (
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_google_id_token,
    verify_password,
)

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
def register(body: RegisterRequest, response: Response, db: Session = Depends(get_db)):
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
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    """Exchange email + password for JWT tokens."""
    user = db.query(User).filter_by(email=body.email.lower()).first()

    # Check both cases with the same error to prevent email enumeration.
    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return _issue_tokens(user, response)


@router.post("/google", response_model=TokenResponse)
def google_auth(body: GoogleAuthRequest, response: Response, db: Session = Depends(get_db)):
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
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Google ID token")

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

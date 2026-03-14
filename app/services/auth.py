"""
Authentication service.

Handles the two concerns that don't belong in a route handler:
  1. Password hashing and verification (bcrypt)
  2. JWT creation and decoding (python-jose)
  3. Google ID token verification (google-auth)
  4. Password-reset token generation and validation

Keeping this in a service makes it easy to unit-test without starting a web server.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import settings

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 7


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt. The salt is embedded in the returned string."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if the plaintext password matches the stored bcrypt hash."""
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(user_id: str) -> str:
    """
    Create a short-lived JWT access token (15 minutes).

    The 'type' claim distinguishes access tokens from refresh tokens so that a
    refresh token cannot be used as an access token and vice versa.
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": user_id, "exp": expire, "type": "access"}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    """Create a long-lived JWT refresh token (7 days), stored in an httpOnly cookie."""
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {"sub": user_id, "exp": expire, "type": "refresh"}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """
    Decode and validate a JWT access token.

    Raises JWTError if the token is expired, malformed, or is not an access token.
    The caller (dependencies.py) converts JWTError into an HTTP 401 response.
    """
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    if payload.get("type") != "access":
        raise JWTError("Token is not an access token")
    return payload


def decode_refresh_token(token: str) -> dict:
    """Decode and validate a JWT refresh token. Raises JWTError on failure."""
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    if payload.get("type") != "refresh":
        raise JWTError("Token is not a refresh token")
    return payload


def _hash_token(raw: str) -> str:
    """Return the SHA-256 hex digest of a raw token string."""
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_reset_token(db: Session, user) -> str:
    """
    Delete any existing reset tokens for the user, create a new one, and
    return the raw (unhashed) token that will be sent in the email link.

    Deleting old tokens means at most one pending reset exists per user.
    """
    from app.models.password_reset_token import PasswordResetToken

    db.query(PasswordResetToken).filter_by(user_id=user.id).delete()
    raw = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(raw),
            expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=settings.RESET_TOKEN_EXPIRE_HOURS),
        )
    )
    db.commit()
    return raw


def validate_reset_token(db: Session, raw_token: str):
    """
    Return the User associated with the token if it is valid, unused, and
    not expired. Return None for any invalid state without leaking which
    condition failed.
    """
    from app.models.password_reset_token import PasswordResetToken
    from app.models.user import User

    record = db.query(PasswordResetToken).filter_by(token_hash=_hash_token(raw_token)).first()
    now = datetime.now(tz=timezone.utc)
    if not record or record.used_at is not None or record.expires_at <= now:
        return None
    return db.get(User, record.user_id)


def consume_reset_token(db: Session, raw_token: str) -> None:
    """Mark the token as used so it cannot be redeemed a second time."""
    from app.models.password_reset_token import PasswordResetToken

    record = db.query(PasswordResetToken).filter_by(token_hash=_hash_token(raw_token)).first()
    if record:
        record.used_at = datetime.now(tz=timezone.utc)
        db.commit()


def verify_google_id_token(id_token: str) -> dict:
    """
    Verify a Google-issued ID token and return its claims.

    The google-auth library makes a network call to Google's public key endpoint
    the first time, then caches the keys. Returns a dict with fields like:
      - 'sub': Google user ID (stable, use this as the google_id)
      - 'email': user's email
      - 'name': user's full name

    Raises google.auth.exceptions.GoogleAuthError if the token is invalid.
    GOOGLE_CLIENT_ID must match the client ID used in the frontend.
    """
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    return google_id_token.verify_oauth2_token(
        id_token,
        google_requests.Request(),
        settings.GOOGLE_CLIENT_ID,
    )

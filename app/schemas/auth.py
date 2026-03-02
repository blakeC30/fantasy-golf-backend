"""
Auth request/response schemas.

These define exactly what the API accepts and returns for authentication
endpoints. Keeping them thin — validation lives in the service layer.
"""

from pydantic import BaseModel


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str


class LoginRequest(BaseModel):
    email: str
    password: str


class GoogleAuthRequest(BaseModel):
    """The Google ID token received by the frontend after the user signs in."""
    id_token: str


class TokenResponse(BaseModel):
    """
    Returned after successful login/register.

    The refresh token is NOT included here — it is sent as an httpOnly cookie
    so JavaScript cannot read it, which prevents XSS token theft.
    """
    access_token: str
    token_type: str = "bearer"

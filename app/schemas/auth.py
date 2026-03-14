"""
Auth request/response schemas.

These define exactly what the API accepts and returns for authentication
endpoints. Keeping them thin — validation lives in the service layer.
"""

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    display_name: str = Field(min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleAuthRequest(BaseModel):
    """The Google ID token received by the frontend after the user signs in."""
    id_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8)


class TokenResponse(BaseModel):
    """
    Returned after successful login/register.

    The refresh token is NOT included here — it is sent as an httpOnly cookie
    so JavaScript cannot read it, which prevents XSS token theft.
    """
    access_token: str
    token_type: str = "bearer"

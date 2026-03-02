"""
Application configuration.

pydantic-settings reads values from the environment (and from a .env file if present).
Any variable defined here can be overridden by setting the matching environment variable.
Copy .env.example to .env and fill in real values before running locally.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Database ---
    # Full PostgreSQL connection string.
    # Format: postgresql://user:password@host:port/dbname
    DATABASE_URL: str = "postgresql://fantasygolf:fantasygolf@localhost:5432/fantasygolf_dev"

    # --- Auth ---
    # Long random string used to sign JWTs. Change this in production.
    SECRET_KEY: str = "change-this-to-a-long-random-secret-key"

    # --- Google OAuth ---
    # Client ID from the Google Cloud Console. Used to verify ID tokens.
    GOOGLE_CLIENT_ID: str = ""

    # --- App ---
    ENVIRONMENT: str = "development"
    DEBUG: bool = True

    # --- CORS ---
    # The frontend origin that is allowed to make cross-origin requests to the API.
    FRONTEND_URL: str = "http://localhost:5173"

    class Config:
        # Load values from a .env file in the current working directory.
        env_file = ".env"
        env_file_encoding = "utf-8"


# A single shared instance used throughout the app.
settings = Settings()

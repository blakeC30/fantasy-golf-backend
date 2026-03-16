"""
Application configuration.

pydantic-settings reads values from the environment (and from a .env file if present).
Any variable defined here can be overridden by setting the matching environment variable.
Copy .env.example to .env and fill in real values before running locally.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Database ---
    # Full PostgreSQL connection string.
    # Format: postgresql://user:password@host:port/dbname
    DATABASE_URL: str = "postgresql://fantasygolf:fantasygolf@localhost:5432/fantasygolf_dev"

    # --- Auth ---
    # Long random string used to sign JWTs. Change this in production.
    SECRET_KEY: str = "change-this-to-a-long-random-secret-key"
    # How long password-reset links are valid. Referenced by the auth service,
    # email body, and frontend UI — change here and it propagates everywhere.
    RESET_TOKEN_EXPIRE_HOURS: int = 1

    # --- Google OAuth ---
    # Client ID from the Google Cloud Console. Used to verify ID tokens.
    GOOGLE_CLIENT_ID: str = ""

    # --- App ---
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    # When True, only platform admins can create leagues. All other users see a
    # "not available yet" message. Flip to False once the platform opens publicly.
    LEAGUE_CREATION_RESTRICTED: bool = False

    # --- CORS ---
    # The frontend origin that is allowed to make cross-origin requests to the API.
    FRONTEND_URL: str = "http://localhost:5173"

    # --- AWS / SES ---
    # Region where SES is configured.
    AWS_REGION: str = "us-east-1"
    # Credentials — leave empty in production to use the EC2 IAM instance role.
    # Set to "test" when pointing at LocalStack.
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    # Override the AWS endpoint URL. Empty = real AWS. Set to http://localstack:4566 in Docker dev.
    AWS_ENDPOINT_URL: str = ""
    # The verified sender address in SES. Must be verified in the AWS console before going to prod.
    SES_FROM_EMAIL: str = "noreply@league-caddie.com"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


# A single shared instance used throughout the app.
settings = Settings()

"""
Metadata Service configuration via pydantic-settings.
All values can be overridden by environment variables or a .env file.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql://postgres:password@localhost:5432/dropboxsim"
    db_pool_min: int = 2
    db_pool_max: int = 10
    db_command_timeout: float = 30.0

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # JWT
    # IMPORTANT: change jwt_secret in production — never use the default
    jwt_secret: str = "dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 days

    # Service-to-service auth (shared secret with block server)
    # Leave empty to disable API key enforcement (dev/test mode)
    service_api_key: str = ""

    # CORS
    cors_origins: list = ["*"]

    # gRPC
    grpc_port: int = 50052

    # Observability
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()

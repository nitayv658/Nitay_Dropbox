"""
Block Server configuration via pydantic-settings.
All values can be overridden by environment variables or a .env file.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql://postgres:password@localhost:5432/dropboxsim"
    db_pool_min: int = 2
    db_pool_max: int = 10
    db_command_timeout: float = 30.0

    # Storage
    block_storage_path: str = "./data/blocks"
    storage_backend: str = "local"  # "local" | "s3"
    block_max_size: int = 4 * 1024 * 1024  # 4 MB

    # AWS S3 (only needed when storage_backend="s3")
    aws_bucket: str = ""
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""

    # Service-to-service auth (shared secret with metadata service)
    # Leave empty to disable API key enforcement (dev/test mode)
    service_api_key: str = ""

    # Observability
    log_level: str = "INFO"

    # Block GC
    gc_interval_seconds: int = 3600  # run every hour

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()

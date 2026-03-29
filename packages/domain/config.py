from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GIS_AGENT_", extra="ignore")

    app_name: str = "GIS Agent"
    debug: bool = True
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./.data/dev.db"
    storage_root: str = ".data"
    execution_mode: str = "inline_mock"
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    @property
    def allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @property
    def uploads_dir(self) -> str:
        return f"{self.storage_root}/uploads"

    @property
    def artifacts_dir(self) -> str:
        return f"{self.storage_root}/artifacts"


@lru_cache
def get_settings() -> Settings:
    return Settings()

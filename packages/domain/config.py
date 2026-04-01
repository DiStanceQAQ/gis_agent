from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GIS_AGENT_", extra="ignore")

    app_name: str = "GIS Agent"
    debug: bool = True
    api_prefix: str = "/api/v1"
    database_url: str = "postgresql+psycopg://gis_agent:gis_agent@localhost:5432/gis_agent"
    storage_root: str = ".data"
    aoi_registry_path: str | None = None
    execution_mode: str = "inline_mock"
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    catalog_stac_url: str = "https://planetarycomputer.microsoft.com/api/stac/v1"
    catalog_live_search: bool = False
    catalog_allow_mock_fallback: bool = True
    catalog_timeout_seconds: int = 20
    catalog_page_size: int = 50
    catalog_max_items: int = 100
    real_pipeline_enabled: bool = False
    real_pipeline_max_items: int = 2
    real_pipeline_max_dimension: int = 256

    @property
    def allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @property
    def uploads_dir(self) -> str:
        return f"{self.storage_root}/uploads"

    @property
    def artifacts_dir(self) -> str:
        return f"{self.storage_root}/artifacts"

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()

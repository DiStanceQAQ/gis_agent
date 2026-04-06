from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GIS_AGENT_", extra="ignore")

    app_name: str = "GIS Agent"
    debug: bool = True
    api_prefix: str = "/api/v1"
    database_url: str = "postgresql+psycopg://gis_agent:gis_agent@localhost:5432/gis_agent"
    storage_root: str = ".data"
    storage_backend: str = "local"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "gis-agent"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_region: str = "us-east-1"
    s3_addressing_style: str = "path"
    aoi_registry_path: str | None = None
    execution_mode: str = "celery_agent"
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    catalog_stac_url: str = "https://planetarycomputer.microsoft.com/api/stac/v1"
    catalog_live_search: bool = False
    catalog_allow_baseline_fallback: bool = True
    catalog_timeout_seconds: int = 20
    catalog_page_size: int = 50
    catalog_max_items: int = 100
    real_pipeline_enabled: bool = True
    real_pipeline_max_items: int = 2
    real_pipeline_max_dimension: int = 256
    llm_provider: str = "openai_compatible"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 2
    llm_temperature: float = 0.2
    llm_parser_enabled: bool = True
    llm_parser_schema_retries: int = 2
    llm_parser_legacy_fallback: bool = False
    llm_planner_enabled: bool = True
    llm_planner_schema_retries: int = 2
    llm_planner_legacy_fallback: bool = False
    llm_recommendation_enabled: bool = True
    llm_recommendation_schema_retries: int = 2
    llm_recommendation_legacy_fallback: bool = True
    llm_step_react_enabled: bool = True
    llm_step_react_schema_retries: int = 1
    llm_step_react_legacy_fallback: bool = True
    agent_max_steps: int = 12
    agent_max_tool_calls: int = 24
    agent_runtime_timeout_seconds: int = 900
    agent_step_react_max_rounds: int = 1
    agent_step_react_timeout_seconds: int = 30
    intent_router_enabled: bool = True
    intent_task_confidence_threshold: float = 0.75
    intent_history_limit: int = 8
    intent_confirmation_keywords: str = "开始执行,按这个执行,确认执行,就按这个来"
    local_files_only_mode: bool = False

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
    def is_s3_storage(self) -> bool:
        return self.storage_backend.lower() == "s3"

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def is_async_execution(self) -> bool:
        return self.execution_mode.startswith("celery")


@lru_cache
def get_settings() -> Settings:
    return Settings()

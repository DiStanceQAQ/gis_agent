from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.routers import artifacts, files, health, messages, sessions, tasks
from packages.domain.config import get_settings
from packages.domain.database import init_db
from packages.domain.services.storage import ensure_storage_dirs


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix=settings.api_prefix)
app.include_router(sessions.router, prefix=settings.api_prefix)
app.include_router(files.router, prefix=settings.api_prefix)
app.include_router(messages.router, prefix=settings.api_prefix)
app.include_router(tasks.router, prefix=settings.api_prefix)
app.include_router(artifacts.router, prefix=settings.api_prefix)


@app.on_event("startup")
def on_startup() -> None:
    ensure_storage_dirs(settings)
    init_db()

from collections.abc import Generator

from packages.domain.config import get_settings
from packages.domain.database import SessionLocal


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def settings_dependency():
    return get_settings()


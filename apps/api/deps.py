from collections.abc import Generator

from sqlalchemy.orm import Session

from packages.domain.config import get_settings
from packages.domain.database import SessionLocal


def get_db() -> Generator[Session, None, None]:
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def settings_dependency():
    return get_settings()

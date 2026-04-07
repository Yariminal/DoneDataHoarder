"""Database engine and session factory."""
from pathlib import Path

from sqlalchemy import create_engine, inspect, Engine
from sqlalchemy.orm import Session, sessionmaker

from datahoarder.db.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def init_db(db_path: Path) -> Engine:
    """Create engine, run migrations, return engine. Call once at startup."""
    global _engine, _SessionLocal

    db_url = f"sqlite:///{db_path.resolve()}"
    _engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False},
        echo=False,
    )

    # Check if the 'sessions' table exists; if not, drop everything and
    # recreate so we get a clean slate with the new session-based schema.
    inspector = inspect(_engine)
    existing_tables = inspector.get_table_names()
    if "files" in existing_tables and "sessions" not in existing_tables:
        # Old schema without sessions — drop all and recreate
        Base.metadata.drop_all(_engine)

    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_session() -> Session:
    """Return a new session. Caller is responsible for commit/close."""
    if _SessionLocal is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _SessionLocal()


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _engine

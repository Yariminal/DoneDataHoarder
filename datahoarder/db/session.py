"""Database engine and session factory."""
from pathlib import Path

from sqlalchemy import create_engine, inspect, Engine
from sqlalchemy.orm import Session, sessionmaker

from datahoarder.db.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def _migrate_add_columns(engine: Engine, inspector) -> None:
    """Add any missing columns to existing tables (SQLite doesn't do this automatically)."""
    from sqlalchemy import text

    # Define expected columns for each table: (table, column, sql_type, default)
    migrations = [
        ("sessions", "preferred_language", "VARCHAR", "'leave_as_is'"),
        ("files", "ai_suggested_name", "VARCHAR", "NULL"),
        ("sessions", "analyze_model", "VARCHAR", "NULL"),
        ("sessions", "propose_model", "VARCHAR", "NULL"),
        ("sessions", "relate_scope", "VARCHAR", "'per_directory'"),
        ("files", "date_created_source", "VARCHAR", "NULL"),
    ]

    for table, column, sql_type, default in migrations:
        if table not in inspector.get_table_names():
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(table)}
        if column not in existing_cols:
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {column} {sql_type} DEFAULT {default}"
                ))


def init_db(db_path: Path) -> Engine:
    """Create engine, run migrations, return engine. Call once at startup."""
    global _engine, _SessionLocal

    db_url = f"sqlite:///{db_path.resolve()}"
    _engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 15},
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

    # Add any missing columns to existing tables
    inspector = inspect(_engine)
    _migrate_add_columns(_engine, inspector)

    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine




def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _engine

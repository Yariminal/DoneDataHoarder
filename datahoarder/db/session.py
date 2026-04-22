"""Database engine and session factory."""
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, Engine, exc as sa_exc
from sqlalchemy.orm import Session, sessionmaker

from datahoarder.db.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def _handle_db_lock(err: Exception, db_path: Path) -> None:
    """Pretty-print a database-locked error with actionable advice."""
    msg = (
        f"\n[ERROR] Database is locked: {db_path}\n\n"
        "Another DataHoarder process may be holding the connection.\n"
        "  • Close any other terminals running datahoarder commands\n"
        "  • If the Web UI is running, stop it (Ctrl+C)\n"
        "  • On Unix:  pkill -f datahoarder\n"
        "  • On Windows:  taskkill /F /IM python.exe  (careful!)\n\n"
        "If you are sure no other process is using the DB, delete the\n"
        f"lock file (if any) and retry: {db_path}.db-journal\n"
    )
    print(msg, file=sys.stderr)
    raise RuntimeError(f"Database locked: {db_path}") from err


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
        ("scan_sessions", "last_scanned_path", "VARCHAR", "NULL"),
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
    try:
        _engine = create_engine(
            db_url,
            connect_args={"check_same_thread": False, "timeout": 15},
            echo=False,
        )
    except sa_exc.OperationalError as exc:
        if "database is locked" in str(exc).lower():
            _handle_db_lock(exc, db_path)
        raise

    try:
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
    except sa_exc.OperationalError as exc:
        if "database is locked" in str(exc).lower():
            _handle_db_lock(exc, db_path)
        raise

    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine




def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _engine

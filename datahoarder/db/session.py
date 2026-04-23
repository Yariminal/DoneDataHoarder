"""Database engine and session factory."""
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, Engine, exc as sa_exc, event
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

    @event.listens_for(_engine, "connect")
    def _set_wal(dbapi_conn, connection_record):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA synchronous=NORMAL")

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
        _migrate_nullable_columns(_engine, inspector)
    except sa_exc.OperationalError as exc:
        if "database is locked" in str(exc).lower():
            _handle_db_lock(exc, db_path)
        raise

    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def _migrate_nullable_columns(engine: Engine, inspector) -> None:
    """Alter columns to nullable where the schema has evolved (SQLite limited support)."""
    from sqlalchemy import text

    # SQLite only supports limited ALTER TABLE; we recreate the table
    # if we need to drop a NOT NULL constraint. For scan_sessions,
    # dropping and recreating is acceptable (it's just scan metadata).
    nullable_migrations = [
        ("scan_sessions", "session_id"),
        ("duplicate_groups", "session_id"),
    ]

    for table, column in nullable_migrations:
        if table not in inspector.get_table_names():
            continue
        cols = inspector.get_columns(table)
        col_info = next((c for c in cols if c["name"] == column), None)
        if col_info and not col_info.get("nullable", True):
            # Column is NOT NULL but should be nullable — we need to recreate the table
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} RENAME TO {table}_old"))
                # Create new table with correct schema (sqlite-specific)
                if table == "scan_sessions":
                    conn.execute(text(
                        f"CREATE TABLE {table} ("
                        f"id INTEGER NOT NULL PRIMARY KEY, "
                        f"session_id VARCHAR(36), "
                        f"root_path VARCHAR NOT NULL, "
                        f"started_at DATETIME, "
                        f"finished_at DATETIME, "
                        f"files_found INTEGER DEFAULT 0, "
                        f"files_new INTEGER DEFAULT 0, "
                        f"files_skipped INTEGER DEFAULT 0, "
                        f"files_error INTEGER DEFAULT 0, "
                        f"completed BOOLEAN DEFAULT 0, "
                        f"last_scanned_path VARCHAR, "
                        f"FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE"
                        f")"
                    ))
                elif table == "duplicate_groups":
                    conn.execute(text(
                        f"CREATE TABLE {table} ("
                        f"id INTEGER NOT NULL PRIMARY KEY, "
                        f"session_id VARCHAR(36), "
                        f"dupe_type VARCHAR NOT NULL, "
                        f"group_hash VARCHAR NOT NULL, "
                        f"keep_file_id INTEGER, "
                        f"FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE, "
                        f"FOREIGN KEY(keep_file_id) REFERENCES files (id)"
                        f")"
                    ))
                conn.execute(text(f"INSERT INTO {table} SELECT * FROM {table}_old"))
                conn.execute(text(f"DROP TABLE {table}_old"))




def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _engine

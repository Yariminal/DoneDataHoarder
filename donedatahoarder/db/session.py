"""Database engine and session factory."""
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, Engine, exc as sa_exc, event
from sqlalchemy.orm import Session, sessionmaker

from donedatahoarder.db.models import Base, DuplicateGroup, File

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def _handle_db_lock(err: Exception, db_path: Path) -> None:
    """Pretty-print a database-locked error with actionable advice."""
    msg = (
        f"\n[ERROR] Database is locked: {db_path}\n\n"
        "Another DoneDataHoarder process may be holding the connection.\n"
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
        ("files", "analysis_outcome", "VARCHAR", "NULL"),
        ("files", "analysis_reason", "VARCHAR", "NULL"),
        ("files", "analysis_evidence_source", "VARCHAR", "NULL"),
        ("files", "analysis_model_tag", "VARCHAR", "NULL"),
        ("files", "analysis_model_digest", "VARCHAR", "NULL"),
        ("files", "analysis_prompt_version", "VARCHAR", "NULL"),
        ("files", "analysis_extractor_version", "VARCHAR", "NULL"),
        ("files", "analysis_content_chars", "INTEGER", "NULL"),
        ("duplicate_members", "distance_to_keeper", "FLOAT", "NULL"),
        ("proposals", "duplicate_group_id", "INTEGER", "NULL"),
        ("proposals", "review_kind", "VARCHAR", "NULL"),
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
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA synchronous=NORMAL")

    try:
        # A pre-session database needs an explicit conversion. Never erase an
        # existing user's file index as a side effect of opening the app.
        inspector = inspect(_engine)
        existing_tables = inspector.get_table_names()
        if "files" in existing_tables and "sessions" not in existing_tables:
            raise RuntimeError(
                "Legacy database has files but no sessions table. Back it up and "
                "migrate it explicitly; automatic opening will not erase it."
            )

        Base.metadata.create_all(_engine)

        # Add any missing columns to existing tables
        inspector = inspect(_engine)
        _migrate_add_columns(_engine, inspector)
        _migrate_nullable_columns(_engine, inspector)
        _migrate_session_scope(_engine)
    except sa_exc.OperationalError as exc:
        if "database is locked" in str(exc).lower():
            _handle_db_lock(exc, db_path)
        raise

    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def _migrate_nullable_columns(engine: Engine, inspector) -> None:
    """Rebuild legacy NOT NULL tables while preserving dependent rows."""
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
            _rebuild_table(engine, table, Base.metadata.tables[table])




def _unique_key_sets(conn, table: str) -> list[list[str]]:
    from sqlalchemy import text

    keys: list[list[str]] = []
    for row in conn.execute(text(f"PRAGMA index_list('{table}')")).fetchall():
        if not row[2]:
            continue
        info = conn.execute(text(f"PRAGMA index_info('{row[1]}')")).fetchall()
        cols = [item[2] for item in sorted(info, key=lambda item: item[0])]
        if cols:
            keys.append(cols)
    return keys


def _rebuild_table(engine: Engine, table_name: str, table) -> None:
    """Transactionally recreate a table without retargeting child FKs."""
    from sqlalchemy.schema import CreateIndex, CreateTable

    legacy = f"{table_name}_legacy"
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        raw.commit()
        old_fk = cursor.execute("PRAGMA foreign_keys").fetchone()[0]
        old_legacy = cursor.execute("PRAGMA legacy_alter_table").fetchone()[0]
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("PRAGMA legacy_alter_table=ON")
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(f"ALTER TABLE {table_name} RENAME TO {legacy}")
            for row in cursor.execute(f"PRAGMA index_list('{legacy}')").fetchall():
                index_name = row[1]
                if not str(index_name).startswith("sqlite_autoindex"):
                    cursor.execute(f'DROP INDEX IF EXISTS "{index_name}"')
            cursor.execute(str(CreateTable(table).compile(dialect=engine.dialect)))
            for index in table.indexes:
                cursor.execute(str(CreateIndex(index).compile(dialect=engine.dialect)))
            old_columns = {row[1] for row in cursor.execute(f"PRAGMA table_info('{legacy}')")}
            shared = [column.name for column in table.columns if column.name in old_columns]
            column_sql = ", ".join(f'"{name}"' for name in shared)
            cursor.execute(
                f"INSERT INTO {table_name} ({column_sql}) "
                f"SELECT {column_sql} FROM {legacy}"
            )
            cursor.execute(f"DROP TABLE {legacy}")
            violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"Migration of {table_name} would break foreign keys: {violations[:3]}"
                )
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            cursor.execute(f"PRAGMA legacy_alter_table={old_legacy}")
            cursor.execute(f"PRAGMA foreign_keys={old_fk}")
            if cursor.execute("PRAGMA foreign_keys").fetchone()[0] != old_fk:
                raise RuntimeError("Could not restore SQLite foreign-key enforcement")
    finally:
        raw.close()


def _migrate_session_scope(engine: Engine) -> None:
    """A session owns its file paths and its duplicate groups."""
    with engine.connect() as conn:
        names = set(inspect(engine).get_table_names())
        rebuild_files = (
            "files" in names
            and ["session_id", "path"] not in _unique_key_sets(conn, "files")
        )
        rebuild_dupes = (
            "duplicate_groups" in names
            and ["session_id", "dupe_type", "group_hash"]
            not in _unique_key_sets(conn, "duplicate_groups")
        )
    if rebuild_files:
        _rebuild_table(engine, "files", File.__table__)
    if rebuild_dupes:
        _rebuild_table(engine, "duplicate_groups", DuplicateGroup.__table__)


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _engine

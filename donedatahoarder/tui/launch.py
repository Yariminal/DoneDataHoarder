"""Lazy terminal entry point; importing the CLI never imports Textual."""
from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
import sys


def default_database() -> Path:
    """Keep the terminal index outside the folder being organized."""
    configured = os.environ.get("XDG_DATA_HOME")
    data_home = (Path(configured) if configured and Path(configured).is_absolute()
                 else Path.home() / ".local" / "share")
    return data_home / "donedatahoarder" / "index.db"


def check_requirements() -> None:
    if sys.version_info < (3, 12):
        raise RuntimeError(
            "The TUI requires Python 3.12 or newer. The existing CLI still supports "
            "Python 3.10+. Install with Python 3.12: python -m pip install 'donedatahoarder[tui]'."
        )
    if any(importlib.util.find_spec(name) is None for name in ("textual", "textual_image")):
        raise RuntimeError(
            "Install the terminal interface first: python -m pip install 'donedatahoarder[tui]' "
            "(from a source checkout: python -m pip install -e '.[tui]')."
        )


def launch(
    root: Path | str | None,
    *,
    session_id: str | None = None,
    db_path: Path | None = None,
    model: str = "gemma3:12b",
    ollama_host: str = "http://localhost:11434",
    workers: int = 1,
    images: str = "auto",
    connect: str | None = None,
    token_file: Path | None = None,
    ca_file: Path | None = None,
    discover: bool = False,
) -> None:
    check_requirements()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("The TUI needs an interactive terminal. Run ddh tui in Foot, Kitty, or another terminal.")
    if root is not None and session_id:
        raise ValueError("Choose a folder or --session, not both.")
    if not connect and (token_file or ca_file):
        raise ValueError("Use --connect with --token-file or --ca-file.")
    if discover and (connect or token_file or ca_file):
        raise ValueError("Choose --discover or the manual --connect options.")
    if (connect or discover) and db_path is not None:
        raise ValueError("Remote sessions use the workstation database; omit --db.")
    if connect and token_file is None:
        raise ValueError("Remote connections require --token-file.")
    if root is not None and not connect and not discover:
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Folder does not exist: {root}")

    # Probe before Textual starts reading terminal input. Import the application
    # only afterwards; image-widget imports may themselves query the terminal.
    from donedatahoarder.tui.images import initialize_images
    capability = initialize_images(images)
    from donedatahoarder.tui.app import DDHApp
    connection = None
    if discover:
        catalog = service = None
    elif connect:
        import hashlib
        from donedatahoarder.remote.client import RemoteConnection, RemoteSessionCatalog
        from donedatahoarder.remote.config import read_token
        state_home = Path(os.environ.get("XDG_STATE_HOME", "")).expanduser()
        if not state_home.is_absolute():
            state_home = Path.home() / ".local" / "state"
        pending_file = state_home / "donedatahoarder" / "remote" / (hashlib.sha256(connect.rstrip('/').encode()).hexdigest() + ".json")
        connection = RemoteConnection(connect, read_token(token_file), ca_file=ca_file,
                                      pending_file=pending_file, guard_directory=pending_file.parent)
        try:
            connection.connect()
            catalog = RemoteSessionCatalog(connection)
            service = catalog.open(root=str(root) if root is not None else None,
                                   session_id=session_id) if root is not None or session_id else None
        except BaseException:
            connection.close()
            raise
    else:
        from donedatahoarder.db.session import init_db
        from donedatahoarder.tui.service import WorkspaceService
        from donedatahoarder.tui.onboarding import SessionCatalog
        database = (db_path or default_database()).expanduser().resolve()
        database.parent.mkdir(parents=True, exist_ok=True)
        init_db(database)
        catalog = SessionCatalog(model=model, workers=workers, ollama_host=ollama_host)
        service = WorkspaceService(
            root=root, session_id=session_id, model=model, workers=workers,
            backend="ollama", ollama_host=ollama_host,
        ) if root is not None or session_id else None
    # Keep file logging, but do not let the CLI's Rich handler corrupt the
    # alternate screen while background jobs emit diagnostics.
    from rich.logging import RichHandler
    logger = logging.getLogger("donedatahoarder")
    console_handlers = [handler for handler in logger.handlers if isinstance(handler, RichHandler)]
    for handler in console_handlers:
        logger.removeHandler(handler)
    try:
        nearby_options = {"discover": True, "remote_root": str(root) if root is not None else None,
                          "remote_session": session_id} if discover else {}
        DDHApp(service, image_capability=capability, catalog=catalog, **nearby_options).run()
    finally:
        if connection is not None:
            connection.close()
        for handler in console_handlers:
            logger.addHandler(handler)

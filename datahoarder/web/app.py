"""
FastAPI application — serves the review UI and REST API.

Usage:
    datahoarder serve --db datahoarder.db --port 8080
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from datahoarder.web.api import router as api_router

_HERE = Path(__file__).parent


def create_app(db_path: Path) -> FastAPI:
    """Build and return the FastAPI application."""
    from datahoarder.db.session import init_db

    init_db(db_path)

    app = FastAPI(
        title="DataHoarder",
        description="AI-powered file organization",
        version="0.1.0",
    )

    # Mount static files and templates
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    templates = Jinja2Templates(directory=str(_HERE / "templates"))

    # Include API routes
    app.include_router(api_router, prefix="/api")

    # Serve the SPA for all non-API routes
    @app.get("/")
    @app.get("/{full_path:path}")
    async def serve_spa(request: Request, full_path: str = ""):
        if full_path.startswith("api/") or full_path.startswith("static/"):
            return None
        return templates.TemplateResponse(request, "index.html")

    return app


def create_default_app() -> FastAPI:
    """Factory for uvicorn CLI usage: reads DB path from config, env, or default."""
    import json
    import os

    # Priority: 1) env var  2) ~/.datahoarder.json  3) default
    db_path_str = os.environ.get("DATAHOARDER_DB", "")
    if not db_path_str:
        config_file = Path.home() / ".datahoarder.json"
        if config_file.exists():
            try:
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
                db_path_str = cfg.get("db_path", "")
            except Exception:
                pass
    db_path = Path(db_path_str) if db_path_str else Path("datahoarder.db")
    return create_app(db_path)

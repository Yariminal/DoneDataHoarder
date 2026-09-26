"""
FastAPI application — serves the review UI and REST API.

Usage:
    datahoarder serve --db donedatahoarder.db --port 8080
"""
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.responses import JSONResponse

from donedatahoarder.web.api import router as api_router

_HERE = Path(__file__).parent


def create_app(db_path: Path) -> FastAPI:
    """Build and return the FastAPI application."""
    from donedatahoarder.db.session import init_db

    init_db(db_path)

    from donedatahoarder import __version__

    app = FastAPI(
        title="DoneDataHoarder",
        description="AI-powered file organization",
        version=__version__,
    )

    # CSRF guard: any webpage can fire fetch() at http://127.0.0.1:<port>,
    # and endpoints like /api/execute or /api/ollama/restart change state.
    # Browsers attach an Origin header to cross-origin requests — reject
    # state-changing methods whose Origin is not this server itself.
    # Requests without an Origin header (curl, CLI tools, same-origin GETs)
    # pass through untouched.
    _LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}

    @app.middleware("http")
    async def _reject_cross_origin_writes(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin:
                parsed = urlparse(origin)
                same_host = parsed.netloc == request.headers.get("host", "")
                local = parsed.hostname in _LOCAL_HOSTNAMES
                if not (same_host or local):
                    return JSONResponse(
                        {"detail": "Cross-origin request blocked"}, status_code=403
                    )
        return await call_next(request)

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
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return templates.TemplateResponse(request, "index.html")

    return app


def create_default_app() -> FastAPI:
    """Factory for uvicorn CLI usage: reads DB path from config, env, or default."""
    import json
    import os

    # Priority: 1) env var  2) ~/.datahoarder.json  3) default
    db_path_str = os.environ.get("DDH_DB", "")
    if not db_path_str:
        config_file = Path.home() / ".datahoarder.json"
        if config_file.exists():
            try:
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
                db_path_str = cfg.get("db_path", "")
            except Exception:
                pass
    db_path = Path(db_path_str) if db_path_str else Path("donedatahoarder.db")
    return create_app(db_path)

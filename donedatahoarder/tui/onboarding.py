"""Read-only startup discovery, independent of terminal rendering and sessions."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session

from donedatahoarder.core.jobs import job_manager
from donedatahoarder.db.models import RunPlan, UserSession
from donedatahoarder.db.session import get_engine

from .service import WorkspaceService, _session_dict


def model_readiness(host: str, model: str | list[str]) -> str:
    """Query installed models only; never generate, pull, or upload any files."""
    try:
        address = urlsplit(host)
    except ValueError:
        return "Use a valid http:// or https:// Ollama address. Metadata only remains available."
    if address.scheme not in {"http", "https"} or not address.hostname:
        return "Use an http:// or https:// Ollama address. Metadata only remains available."
    requested = list(dict.fromkeys(value.strip() for value in ([model] if isinstance(model, str) else model) if value.strip()))
    if not requested:
        return "Enter a model name. Metadata only does not require Ollama."
    try:
        with httpx.Client(timeout=2, follow_redirects=False, trust_env=False) as client:
            response = client.get(host.rstrip("/") + "/api/tags")
            response.raise_for_status()
            data = response.json()
        models = data.get("models", [])
        installed = {str(item.get("name") or item.get("model") or "")
                     for item in models if isinstance(item, dict)}
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return "Ollama is unavailable. Start it with ollama serve, or use Metadata only."
    normalized = {name if ":" in name else name + ":latest" for name in installed}
    missing = [name for name in requested if (name if ":" in name else name + ":latest") not in normalized]
    if not missing:
        return f"Ollama ready · {', '.join(requested)} {'is' if len(requested) == 1 else 'are'} installed. Nothing has been started."
    return f"Ollama is reachable; {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not installed. Install separately, or use Metadata only."


def _server_label(host: str) -> str:
    """Identify the target without printing any URL credentials or query data."""
    try:
        parsed = urlsplit(host)
        return f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    except ValueError:
        return "configured Ollama server"


class SessionCatalog:
    """Browse an initialized database without creating an empty session."""

    def __init__(self, *, model: str, workers: int, ollama_host: str):
        self.model, self.workers, self.ollama_host = model, workers, ollama_host
        self.engine = get_engine()

    def _check_database(self) -> None:
        if get_engine() is not self.engine:
            raise RuntimeError("The active database changed; reopen the terminal interface")

    def list_sessions(self) -> list[dict]:
        self._check_database()
        with Session(self.engine) as db:
            return [_session_dict(owner) for owner in db.query(UserSession)
                    .order_by(UserSession.updated_at.desc()).limit(100)]

    def open(self, *, root: str | None = None, session_id: str | None = None,
             model: str | None = None) -> WorkspaceService:
        self._check_database()
        if job_manager.get_active() is not None or job_manager.has_live_workers():
            raise ValueError("Stop the current pipeline and wait for its worker before switching sessions.")
        if root is not None and not root.strip():
            raise ValueError("Choose a collection folder first.")
        return WorkspaceService(root=Path(root) if root is not None else None,
                                session_id=session_id, model=model or self.model,
                                workers=self.workers, ollama_host=self.ollama_host)

    def readiness(self, model: str) -> str:
        return f"New collection · {_server_label(self.ollama_host)} · " + model_readiness(self.ollama_host, model)

    def session_readiness(self, session_id: str) -> str:
        """Check the exact settings a resumed plan (or next new run) will use."""
        self._check_database()
        with Session(self.engine) as db:
            owner = db.get(UserSession, session_id)
            if owner is None:
                raise ValueError("Saved session no longer exists")
            plan = (db.query(RunPlan).filter(RunPlan.session_id == session_id)
                    .order_by(RunPlan.created_at.desc()).first())
            if plan is not None and plan.state != "completed":
                options = json.loads(plan.options_json or "{}")
                steps = json.loads(plan.steps_json or "[]")[plan.current_index:]
                host = options.get("ollama_host", "http://localhost:11434")
                backend = options.get("backend", "ollama")
                models = []
                if "analyze" in steps:
                    models.append(options.get("analyze_model", "gemma3:12b"))
                if any(step in steps for step in ("relate", "propose", "organize")):
                    models.append(options.get("propose_model", "gemma3:12b"))
                label = "Resume saved plan"
            else:
                host, backend = self.ollama_host, owner.backend
                models = [owner.analyze_model or owner.model, owner.propose_model or owner.model]
                label = "Next new run"
        if backend != "ollama":
            return f"{label} uses a cloud backend. Open it in the CLI/web interface."
        if not models:
            return f"{label} has no remaining AI stages. Ollama is not required."
        return f"{label} · {_server_label(host)} · " + model_readiness(host, models)

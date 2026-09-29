"""Narrow authenticated API for a workstation-owned terminal session.

This application intentionally does not mount the browser API. Binding and TLS
policy belong to the CLI; this factory is also usable behind an SSH tunnel.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path
import socket
import threading
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError
from sqlalchemy.orm import Session

from donedatahoarder import __version__
from donedatahoarder.core.process_lock import OperationBusyError
from donedatahoarder.core.review import ReviewError
from donedatahoarder.core.scanner import _is_link_or_reparse
from donedatahoarder.db.models import UserSession
from donedatahoarder.db.session import get_engine, init_db
from donedatahoarder.tui.onboarding import SessionCatalog
from donedatahoarder.tui.service import WorkspaceService, _session_dict

from .receipts import ReceiptConflict, ReceiptStore
from .previews import install_preview_route, preview_revision
from .pairing import PairingStore, PairingError, PairingRateLimited, MAX_PAIR_BODY_BYTES

logger = logging.getLogger("donedatahoarder.remote")
PREFIX = "/remote/v1"
PATH_FLAVOR = "windows" if os.name == "nt" else "posix"


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Start(_Params):
    metadata_only: StrictBool = False


class _Resume(_Params):
    retry_errors: StrictBool = False


class _Proposal(_Params):
    proposal_id: StrictInt = Field(gt=0)


class _Approve(_Proposal):
    review_token: StrictStr | None = Field(default=None, min_length=1, max_length=128)


class _Edit(_Proposal):
    value: StrictStr = Field(min_length=1, max_length=4096)


class _ApproveClear(_Params):
    min_confidence: float = Field(default=0.9, ge=0, le=1, allow_inf_nan=False)


class _Confirmed(_Params):
    token: StrictStr = Field(min_length=1, max_length=128)
    confirmed: StrictBool = False


class _History(_Params):
    limit: StrictInt = Field(default=100, ge=1, le=1000)


class _Settings(_Params):
    model: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    workers: StrictInt | None = Field(default=None, ge=1, le=32)


COMMANDS: dict[str, type[_Params]] = {
    "start_pipeline": _Start, "resume_pipeline": _Resume,
    "pause_pipeline": _Params, "cancel_pipeline": _Params,
    "approve": _Approve, "reject": _Proposal, "edit": _Edit,
    "approve_clear": _ApproveClear, "preview": _Params,
    "apply": _Confirmed, "history": _History, "undo_preview": _Params,
    "undo": _Confirmed, "preflight": _Start, "update_settings": _Settings,
}
READ_COMMANDS = {"preview", "history", "undo_preview", "preflight"}


def _bounded_snapshot(result: dict, *, filesystem_available: bool = True) -> dict:
    """Bound descriptive text and group membership without changing path values."""
    text_budget = 6 * 1024 * 1024
    descriptive = {"text", "ai_description", "reasoning", "reason", "error_message",
                   "analysis_reason", "keeper_description"}
    member_budget = 512
    visible = {file["id"] for file in result["files"]}
    visible.update(proposal["file_id"] for proposal in result["proposals"])
    for group in [*result["duplicates"], *result["collections"]]:
        members = group["members"]
        preferred = visible | {group.get("keep_file_id")}
        ordered = sorted(members, key=lambda file: file["id"] not in preferred)
        chosen = ordered[:min(128, member_budget)]
        member_budget -= len(chosen)
        group["member_count"] = len(members)
        group["members_truncated"] = len(chosen) < len(members)
        group["members"] = chosen

    def trim(value):
        nonlocal text_budget
        if isinstance(value, list):
            for item in value:
                trim(item)
        elif isinstance(value, dict):
            for key, item in list(value.items()):
                if key in descriptive and isinstance(item, str):
                    encoded = item.encode("utf-8")
                    maximum = min(16 * 1024, text_budget)
                    if len(encoded) > maximum:
                        value[key] = encoded[:maximum].decode("utf-8", errors="ignore")
                        value[key + "_truncated"] = True
                    text_budget -= min(len(encoded), maximum)
                elif key == "member_ids" and isinstance(item, list) and len(item) > 2000:
                    value[key] = item[:2000]
                    value["member_ids_truncated"] = True
                else:
                    trim(item)
    trim(result)
    for file in result["files"]:
        file["preview_revision"] = preview_revision(file) if filesystem_available else "unavailable"
    for group in [*result["duplicates"], *result["collections"]]:
        for file in group["members"]:
            file["preview_revision"] = preview_revision(file) if filesystem_available else "unavailable"
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 12 * 1024 * 1024:
        raise HTTPException(413, "Session page is too large; request fewer rows")
    return result


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    command: StrictStr = Field(min_length=1, max_length=40)
    params: dict[str, Any] = Field(default_factory=dict)


class _OpenSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    root: StrictStr | None = Field(default=None, min_length=1, max_length=4096)
    session_id: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    model: StrictStr | None = Field(default=None, min_length=1, max_length=200)


def _absolute(value: str | Path) -> Path:
    """Authorize saved paths even when the external drive is disconnected."""
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise ReviewError("Use an absolute workstation path without parent traversal", 403)
    return path


def _unlinked_absolute(value: str | Path, *, require_directory: bool) -> Path:
    """Reject links at every component, including an allowed root's ancestors."""
    path = _absolute(value)
    try:
        for component in (path, *path.parents):
            try:
                component.lstat()
            except FileNotFoundError:
                # Stored rows can outlive a file or a detached drive. Missing
                # paths are not links; operations needing a directory still
                # reject them below, and preview requires a real regular file.
                continue
            if _is_link_or_reparse(component):
                raise ReviewError("Symlinks and junctions are not available to remote sessions", 403)
        if require_directory and not path.is_dir():
            raise ReviewError("Workstation collection folder is unavailable", 403)
        return path.resolve(strict=require_directory)
    except (OSError, RuntimeError) as exc:
        raise ReviewError("Workstation path is unavailable", 403) from exc


class _Scope:
    def __init__(self, roots: list[Path]):
        if not roots:
            raise ValueError("Configure at least one allowed workstation folder")
        self.roots = tuple(dict.fromkeys(_absolute(root) for root in roots))

    def lexical(self, value: str | Path) -> Path:
        path = _absolute(value)
        if any(path.is_relative_to(root) for root in self.roots):
            return path
        raise ReviewError("Collection is outside the workstation's allowed folders", 403)

    def root(self, value: str | Path) -> Path:
        self.lexical(value)
        path = _unlinked_absolute(value, require_directory=True)
        for root in self.roots:
            # Revalidate the configured folder in case it has been replaced.
            checked = _unlinked_absolute(root, require_directory=True)
            if checked == root and path.is_relative_to(root):
                return path
        raise ReviewError("Collection is outside the workstation's allowed folders", 403)

    def storage(self, value: str | Path) -> dict:
        self.lexical(value)
        try:
            self.root(value)
        except ReviewError:
            return {"available": False,
                    "message": "External collection folder is unavailable; reconnect the drive."}
        return {"available": True, "message": "Workstation collection is available"}

    def file(self, value: str, root: Path, *, filesystem_available: bool = True) -> Path:
        path = (_unlinked_absolute(value, require_directory=False)
                if filesystem_available else _absolute(value))
        if not path.is_relative_to(root):
            raise ReviewError("File is outside its workstation collection", 403)
        return path


def create_app(db_path: Path, *, token: str, allowed_roots: list[Path],
               model: str = "gemma3:12b", workers: int = 1,
               ollama_host: str = "http://localhost:11434",
               name: str | None = None, pairing_store: PairingStore | None = None,
               pairing_path: Path | None = None) -> FastAPI:
    """Create one authenticated daemon; never mount the general web routes."""
    if not isinstance(token, str) or len(token) < 32 or any(char.isspace() or ord(char) < 32 for char in token):
        raise ValueError("The remote token must contain at least 32 non-whitespace characters")
    _Settings(model=model, workers=workers)
    if not model.strip() or any(ord(char) < 32 for char in model):
        raise ValueError("Choose a non-empty model name")
    scope = _Scope(allowed_roots)
    database = Path(db_path).expanduser().resolve()
    from donedatahoarder.core.undo_log import get_datahoarder_dir
    from .config import validate_control_paths
    if pairing_store is not None and pairing_path is not None:
        raise ValueError("Choose one pairing store or pairing path")
    controls = {"database": database, "recovery journal": get_datahoarder_dir(create=False)}
    if pairing_store is not None or pairing_path is not None:
        controls["paired device credentials"] = pairing_store.path if pairing_store else pairing_path
    validate_control_paths(controls, list(scope.roots))
    database.parent.mkdir(parents=True, exist_ok=True)
    engine = init_db(database)
    catalog = SessionCatalog(model=model, workers=workers, ollama_host=ollama_host)
    receipts = ReceiptStore(Path(str(database) + ".remote-receipts.sqlite3"))
    if pairing_path is not None:
        pairing_store = PairingStore(pairing_path, receipts.server_id)
    if pairing_store is not None and pairing_store.server_id != receipts.server_id:
        raise ValueError("Pairing store belongs to another workstation identity")
    app = FastAPI(title="DoneDataHoarder Remote", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None,
                  redirect_slashes=False)
    expected = ("Bearer " + token).encode("utf-8")
    command_lock = threading.Lock()

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        supplied = request.headers.get("authorization", "").encode("utf-8")
        pairing_request = (pairing_store is not None and request.method == "POST"
                           and request.url.path == PREFIX + "/pair")
        authenticated = hmac.compare_digest(supplied, expected)
        if (not authenticated and not pairing_request and pairing_store is not None
                and request.url.scheme == "https"):
            header = request.headers.get("authorization", "")
            authenticated = header.startswith("Bearer ") and pairing_store.authenticate(header[7:])
        if not authenticated and not pairing_request:
            return JSONResponse({"detail": "Workstation authentication required"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ReviewError)
    async def review_error(_request: Request, exc: ReviewError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(OperationBusyError)
    async def busy_error(_request: Request, exc: OperationBusyError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ReceiptConflict)
    async def receipt_conflict(_request: Request, exc: ReceiptConflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def check_database():
        if get_engine() is not engine:
            raise HTTPException(503, "The workstation database changed; restart the remote server")

    def owner_root(session_id: str, *, require_available: bool = False) -> Path:
        check_database()
        with Session(engine) as db:
            owner = db.get(UserSession, session_id)
            if owner is None:
                raise ReviewError("Session not found", 404)
            if owner.backend != "ollama":
                raise ReviewError("Remote terminal sessions require the Ollama backend", 400)
            return (scope.root(owner.root_path) if require_available
                    else scope.lexical(owner.root_path))

    def service(session_id: str, *, require_available: bool = True) -> WorkspaceService:
        owner_root(session_id, require_available=require_available)
        return WorkspaceService(session_id=session_id, ollama_host=ollama_host)

    def authorize_file(session_id: str, file_id: int) -> dict:
        workspace = service(session_id)
        root = owner_root(session_id)
        file = workspace.get_file(file_id)
        scope.file(file["path"], root)
        return {**file, "root_path": str(root), "preview_revision": preview_revision(file)}

    app.state.remote_service = service
    app.state.remote_authorize_file = authorize_file
    app.state.remote_receipts = receipts
    app.state.remote_allowed_roots = scope.roots
    app.state.remote_pairing = pairing_store

    if pairing_store is not None:
        @app.post(PREFIX + "/pair")
        async def pair(request: Request):
            # Never accept an invitation secret on an unencrypted transport,
            # even when the normal authenticated API uses an SSH/HTTP tunnel.
            if request.url.scheme != "https":
                raise HTTPException(400, "Pairing requires verified HTTPS")
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                raise HTTPException(415, "Pairing requires JSON")
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_PAIR_BODY_BYTES:
                    raise HTTPException(413, "Pairing request is too large")
                body.extend(chunk)
            try:
                payload = json.loads(body)
                if (not isinstance(payload, dict) or set(payload) != {"secret", "device_name"}
                        or not isinstance(payload["secret"], str)
                        or not isinstance(payload["device_name"], str)):
                    raise ValueError("Invalid pairing fields")
            except (ValueError, UnicodeDecodeError, RecursionError):
                # Framework validation errors can echo submitted inputs. This
                # endpoint must never reflect a secret in diagnostics.
                raise HTTPException(422, "Invalid pairing request") from None
            try:
                return pairing_store.redeem(payload["secret"], payload["device_name"])
            except PairingRateLimited:
                raise HTTPException(429, "Too many pairing attempts; try again in one minute",
                                    headers={"Retry-After": "60"}) from None
            except PairingError:
                raise HTTPException(401, "Pairing was not accepted; request a new invitation on the workstation") from None

    def execute(request_id: str, payload: dict, operation, *, mutation: bool = True) -> dict:
        payload = {**payload, "mutation": mutation}
        existing, claimed = receipts.claim(request_id, payload)
        if not claimed:
            return existing
        locked = mutation and command_lock.acquire(blocking=False)
        if mutation and not locked:
            return receipts.finish(request_id, error={"detail": "Another remote command is still completing", "status_code": 409})
        try:
            return dispatch(request_id, payload, operation, mutation=mutation)
        finally:
            if locked:
                command_lock.release()

    def dispatch(request_id: str, payload: dict, operation, *, mutation: bool) -> dict:
        try:
            if (mutation and payload["action"] not in {"pause_pipeline", "cancel_pipeline"}
                    and receipts.uncertain_for(payload.get("session_id"), payload.get("root"))):
                raise ReviewError("An earlier command has an uncertain outcome. Inspect this session on the workstation before more changes")
            result = operation()
        except (ReviewError, HTTPException) as exc:
            detail = str(exc) if isinstance(exc, ReviewError) else str(exc.detail)
            return receipts.finish(request_id, error={"detail": detail, "status_code": exc.status_code})
        except OperationBusyError as exc:
            return receipts.finish(request_id, error={"detail": str(exc), "status_code": 409})
        except ValueError as exc:
            if mutation:
                logger.exception("Remote mutation failed after dispatch (%s)", request_id)
                return receipts.finish(request_id, uncertain=True, error={
                    "detail": "The workstation could not confirm the command outcome. Inspect this session and workstation logs before more changes", "status_code": 409})
            return receipts.finish(request_id, error={"detail": str(exc), "status_code": 400})
        except Exception:
            logger.exception("Remote command failed (%s)", request_id)
            return receipts.finish(request_id, uncertain=mutation, error={
                "detail": "The workstation could not confirm the command outcome. Inspect this session and workstation logs before more changes", "status_code": 409})
        return receipts.finish(request_id, result=result)

    @app.get(PREFIX + "/hello")
    def hello():
        check_database()
        return {"protocol": 1, "name": name or socket.gethostname(), "version": __version__,
                "server_id": receipts.server_id,
                "path_flavor": PATH_FLAVOR, "default_root": str(scope.roots[0]),
                "model": model, "workers": workers}

    @app.get(PREFIX + "/sessions")
    def sessions():
        check_database()
        result = []
        with Session(engine) as db:
            owners = db.query(UserSession).order_by(UserSession.updated_at.desc())
            for owner in owners:
                if owner.backend != "ollama":
                    continue
                try:
                    scope.lexical(owner.root_path)
                except ReviewError:
                    continue
                result.append({**_session_dict(owner), "storage": scope.storage(owner.root_path)})
                if len(result) >= 100:
                    break
        return result

    @app.get(PREFIX + "/readiness")
    def readiness(model: str = Query(default=model, min_length=1, max_length=200)):
        check_database()
        return {"message": catalog.readiness(model)}

    @app.get(PREFIX + "/sessions/{session_id}/readiness")
    def session_readiness(session_id: str):
        owner_root(session_id)
        return {"message": catalog.session_readiness(session_id)}

    @app.post(PREFIX + "/sessions")
    def open_session(body: _OpenSession):
        check_database()
        if body.root is not None and body.session_id is not None:
            raise HTTPException(422, "Choose a workstation folder or a session ID, not both")
        if body.session_id:
            owner_root(body.session_id)
            root = None
        else:
            root = scope.root(body.root or scope.roots[0])
        payload = {"action": "open_session", "session_id": body.session_id,
                   "root": str(root) if root is not None else None, "model": body.model or model}

        def open_workspace():
            if body.session_id:
                workspace = service(body.session_id, require_available=False)
            else:
                workspace = catalog.open(root=str(root), model=body.model)
            return {"session_id": workspace.session_id}

        return execute(str(body.request_id), payload, open_workspace,
                       mutation=body.session_id is None)

    @app.get(PREFIX + "/sessions/{session_id}/snapshot")
    def snapshot(session_id: str, limit: int = Query(default=500, ge=1, le=2000),
                 offset: int = Query(default=0, ge=0)):
        root = owner_root(session_id)
        storage = scope.storage(root)
        available = storage["available"]
        result = service(session_id, require_available=False).snapshot(
            limit=limit, offset=offset, filesystem_available=available)
        for file in result["files"]:
            scope.file(file["path"], root, filesystem_available=available)
        for group in [*result["duplicates"], *result["collections"]]:
            for file in group["members"]:
                scope.file(file["path"], root, filesystem_available=available)
        # Proposals and duplicate keepers may be outside the current files
        # page. Their fallback image sources need the same scope and live
        # cache identity as normal file rows.
        for proposal in result["proposals"]:
            source = proposal["file_path"]
            scope.file(source, root, filesystem_available=available)
            proposal["preview_revision"] = (preview_revision({"path": source})
                                              if available else "unavailable")
            evidence = proposal.get("duplicate_evidence")
            if evidence and evidence.get("keeper_path"):
                keeper = evidence["keeper_path"]
                scope.file(keeper, root, filesystem_available=available)
                evidence["keeper_preview_revision"] = (preview_revision({"path": keeper})
                                                        if available else "unavailable")
        result["path_flavor"] = PATH_FLAVOR
        result["storage"] = storage
        return _bounded_snapshot(result, filesystem_available=available)

    @app.get(PREFIX + "/sessions/{session_id}/files/{file_id}")
    def file_detail(session_id: str, file_id: int):
        root = owner_root(session_id)
        available = scope.storage(root)["available"]
        result = service(session_id, require_available=False).get_file(file_id)
        scope.file(result["path"], root, filesystem_available=available)
        result["root_path"] = str(root)
        result["preview_revision"] = preview_revision(result) if available else "unavailable"
        for key in ("text", "ai_description", "error_message", "analysis_reason"):
            if isinstance(result.get(key), str):
                encoded = result[key].encode("utf-8")
                if len(encoded) > 16 * 1024:
                    result[key] = encoded[:16 * 1024].decode("utf-8", errors="ignore")
                    result[key + "_truncated"] = True
        return result

    @app.post(PREFIX + "/sessions/{session_id}/commands")
    def command(session_id: str, body: _Command):
        workspace = service(session_id, require_available=body.command not in {
            "pause_pipeline", "cancel_pipeline", "history"})
        param_type = COMMANDS.get(body.command)
        if param_type is None:
            raise HTTPException(422, "Unsupported remote command")
        try:
            params = param_type.model_validate(body.params).model_dump()
        except ValidationError as exc:
            raise HTTPException(422, "Invalid command parameters: " + str(exc)) from exc
        payload = {"action": body.command, "session_id": session_id,
                   "root": str(owner_root(session_id)), "params": params}
        return execute(str(body.request_id), payload,
                       lambda: getattr(workspace, body.command)(**params),
                       mutation=body.command not in READ_COMMANDS)

    @app.get(PREFIX + "/commands/{request_id}")
    def command_status(request_id: UUID):
        check_database()
        key = str(request_id)
        payload = receipts.payload(key)
        if payload is None:
            raise HTTPException(404, "Command receipt not found")
        if payload.get("session_id"):
            owner_root(payload["session_id"])
        elif payload.get("root"):
            scope.lexical(payload["root"])
        return receipts.get(key)

    install_preview_route(app)
    return app

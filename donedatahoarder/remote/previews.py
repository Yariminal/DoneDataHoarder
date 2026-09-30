"""Session-scoped workstation previews and a transport-neutral TUI source.

Importing this module never imports Textual or probes terminal input. The server
decodes/crops originals; the laptop receives only a bounded PNG. Original paths
remain workstation metadata and are never opened on the laptop.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import math
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from donedatahoarder.tui.images import PreparedImage, PreviewError, _signature, prepare_image

MAX_PNG_BYTES = 8 * 1024 * 1024
MAX_SOURCE_PIXELS = 64_000_000


def _geometry(max_size, zoom, center):
    if (len(max_size) != 2 or any(type(side) is not int or not 1 <= side <= 2048
                                  for side in max_size)):
        raise PreviewError("Preview size must be 1–2048 pixels on each side")
    if not math.isfinite(zoom) or not 1 <= zoom <= 16:
        raise PreviewError("Preview zoom must be between 1 and 16")
    if len(center) != 2 or any(not math.isfinite(value) or not 0 <= value <= 1 for value in center):
        raise PreviewError("Preview center must be normalized coordinates from 0 to 1")


def _revision(signature: tuple) -> str:
    return hashlib.sha256(repr(signature).encode("utf-8")).hexdigest()


def preview_revision(file: dict) -> str:
    """Return live stat identity, or a stable unavailable marker for snapshots.

    This is a cache invalidation hint, not content evidence or authorization.
    The preview route independently authorizes and validates the actual source.
    """
    try:
        return _revision(_signature(Path(file["path"])))
    except (KeyError, TypeError, ValueError, OSError, PreviewError):
        return "unavailable"


@dataclass(frozen=True)
class RemoteImageSource:
    connection: Any = field(compare=False, repr=False)
    session_id: str
    file_id: int
    revision: str
    generation: int = 0
    _connection_identity: int = field(init=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "_connection_identity", id(self.connection))

    def prepare(self, *, max_size: tuple[int, int] = (1024, 768),
                zoom: float = 1.0, center: tuple[float, float] = (0.5, 0.5),
                cancelled: Callable[[], bool] | None = None) -> PreparedImage:
        _geometry(max_size, zoom, center)
        if cancelled is not None and cancelled():
            raise PreviewError("Preview cancelled")
        try:
            payload = self.connection.request_json(
                "GET", f"/sessions/{quote(self.session_id, safe='')}/files/{self.file_id}/preview",
                params={"width": max_size[0], "height": max_size[1], "zoom": zoom,
                        "x": center[0], "y": center[1]},
            )
        except (ValueError, OSError, RuntimeError) as exc:
            raise PreviewError(f"Workstation preview unavailable: {exc}") from exc
        if cancelled is not None and cancelled():
            raise PreviewError("Preview cancelled")
        result = _decode_response(payload, max_size=max_size, zoom=zoom,
                                  file_id=self.file_id)
        if cancelled is not None and cancelled():
            result.image.close()
            raise PreviewError("Preview cancelled")
        return result


def remote_source(connection, session_id: str, file: dict) -> RemoteImageSource:
    """Build a source from an indexed file ID; the supplied path is never used."""
    file_id = file.get("id")
    if type(file_id) is not int or file_id < 1 or not session_id:
        raise PreviewError("A session and indexed image are required for remote preview")
    if file.get("session_id", session_id) != session_id:
        raise PreviewError("Image does not belong to this session")
    # Older snapshots may lack the live stat hint. This fallback invalidates on
    # index updates, but cannot detect an unindexed edit on the workstation.
    revision = file.get("preview_revision")
    if revision is None:
        revision = repr((file.get("size_bytes"), file.get("date_modified")))
    return RemoteImageSource(connection, session_id, file_id, str(revision),
                             getattr(connection, "generation", 0))


def _decode_response(payload, *, max_size, zoom, file_id) -> PreparedImage:
    from PIL import Image, UnidentifiedImageError

    pixels = None
    try:
        if not isinstance(payload, dict):
            raise ValueError("response must be an object")
        encoded = payload["image"]
        if not isinstance(encoded, str) or len(encoded) > ((MAX_PNG_BYTES + 2) // 3) * 4:
            raise ValueError("PNG exceeds the transfer limit")
        binary = base64.b64decode(encoded, validate=True)
        if len(binary) > MAX_PNG_BYTES:
            raise ValueError("PNG exceeds the transfer limit")
        original_size = payload["original_size"]
        crop = payload["crop_box"]
        if (not isinstance(original_size, (list, tuple)) or len(original_size) != 2
                or any(type(side) is not int or side < 1 for side in original_size)
                or original_size[0] * original_size[1] > MAX_SOURCE_PIXELS):
            raise ValueError("invalid original image dimensions")
        if (not isinstance(crop, (list, tuple)) or len(crop) != 4
                or any(type(side) is not int for side in crop)
                or not 0 <= crop[0] < crop[2] <= original_size[0]
                or not 0 <= crop[1] < crop[3] <= original_size[1]):
            raise ValueError("invalid original-pixel crop")
        image_format = payload.get("format", "image")
        if not isinstance(image_format, str) or len(image_format) > 32:
            raise ValueError("invalid image format")
        animated = payload.get("is_animated", False)
        if type(animated) is not bool:
            raise ValueError("invalid animation metadata")
        with Image.open(io.BytesIO(binary)) as thumbnail:
            if (thumbnail.format != "PNG" or thumbnail.width > max_size[0]
                    or thumbnail.height > max_size[1]
                    or thumbnail.width * thumbnail.height > 2048 * 2048
                    or getattr(thumbnail, "is_animated", False)):
                raise ValueError("response is not a bounded, static PNG")
            pixels = thumbnail.convert("RGBA")
        # This synthetic identity is never a local path to the original.
        return PreparedImage(pixels, Path(f"remote-preview-{file_id}"),
                             tuple(original_size), tuple(crop), zoom,
                             image_format, animated)
    except (KeyError, TypeError, ValueError, OSError, binascii.Error,
            Image.DecompressionBombError, UnidentifiedImageError) as exc:
        if pixels is not None:
            pixels.close()
        raise PreviewError(f"Invalid workstation preview: {exc}") from exc


def _authorized_path(file: dict) -> Path:
    """Reject escapes and links, including Windows junction/reparse ancestry."""
    from donedatahoarder.core.scanner import _is_link_or_reparse

    try:
        source, root = Path(file["path"]), Path(file["root_path"])
        if not source.is_absolute() or not root.is_absolute() or ".." in source.parts:
            raise PreviewError("Preview source is outside the session folder")
        source.relative_to(root)
        # Check every ancestor, including the root: resolving first would hide
        # a junction or symlink which redirects a formerly indexed folder.
        for part in (source, *source.parents):
            if _is_link_or_reparse(part):
                raise PreviewError("Preview source has a symlink or junction")
        source.resolve(strict=True).relative_to(root.resolve(strict=True))
        if not stat.S_ISREG(source.stat().st_mode):
            raise PreviewError("Preview requires a regular image file")
        return source.resolve(strict=True)
    except PreviewError:
        raise
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise PreviewError("Preview source is missing or outside the session folder") from exc


def install_preview_route(app) -> None:
    """Add the route to the authenticated remote app, never the legacy web app."""
    from fastapi import HTTPException, Query
    from fastapi.responses import JSONResponse

    @app.get("/remote/v1/sessions/{session_id}/files/{file_id}/preview")
    def file_preview(session_id: str, file_id: int,
                     width: int = Query(1024, ge=1, le=2048),
                     height: int = Query(768, ge=1, le=2048),
                     zoom: float = Query(1, ge=1, le=16),
                     x: float = Query(0.5, ge=0, le=1),
                     y: float = Query(0.5, ge=0, le=1)):
        file = app.state.remote_authorize_file(session_id, file_id)
        if not str(file.get("mime_type", "")).startswith("image/"):
            raise HTTPException(415, "This indexed file is not an image")
        prepared = None
        try:
            _geometry((width, height), zoom, (x, y))
            source = _authorized_path(file)
            signature = _signature(source)
            prepared = prepare_image(source, max_size=(width, height), zoom=zoom, center=(x, y))
            # Decode verifies its own opened descriptor. Also compare against
            # the identity authorized above so a replacement between scope
            # validation and decode cannot return another source's pixels.
            if (prepared.path != source or prepared.source_signature != signature
                    or _authorized_path(file) != source or _signature(source) != signature):
                raise PreviewError("Image changed while preparing it; select it again")
            with io.BytesIO() as output:
                prepared.image.save(output, format="PNG")
                if output.tell() > MAX_PNG_BYTES:
                    raise PreviewError("Preview exceeds the transfer limit; use a smaller preview")
                encoded = base64.b64encode(output.getvalue()).decode("ascii")
            return JSONResponse({
                "image": encoded, "width": prepared.image.width, "height": prepared.image.height,
                "original_size": prepared.original_size, "crop_box": prepared.crop_box,
                "format": prepared.format, "is_animated": prepared.is_animated,
                "revision": _revision(signature),
            }, headers={"Cache-Control": "no-store"})
        except (PreviewError, ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            if prepared is not None:
                prepared.image.close()

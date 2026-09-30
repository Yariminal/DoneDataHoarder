"""Bounded local previews and optional terminal graphics for the TUI.

Call initialize_images() before App.run(), while DDH still owns terminal input.
Pillow, Textual, and textual-image are deliberately imported lazily so importing
the core CLI never probes the terminal or raises its Python requirement.
"""
from __future__ import annotations

import importlib
import math
import os
import stat
import subprocess
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable


class PreviewError(ValueError):
    """An image cannot safely be decoded for a terminal preview."""


@dataclass(frozen=True)
class ImageCapabilities:
    renderer: str = "off"
    message: str = "Image renderer not initialized. Open original to inspect."
    cell_width: int = 10
    cell_height: int = 20


@dataclass(frozen=True)
class PreparedImage:
    image: Any  # PIL.Image.Image; keeping Pillow optional at module-import time.
    path: Path
    original_size: tuple[int, int]
    crop_box: tuple[int, int, int, int]
    zoom: float
    format: str
    is_animated: bool = False
    source_signature: tuple | None = None


@runtime_checkable
class PreviewSource(Protocol):
    """A stable, comparable source which supplies bounded, owned preview pixels.

    Implementations may fetch a workstation thumbnail without mapping its path
    onto the client. They must change identity when the underlying file changes.
    """

    def prepare(self, *, max_size: tuple[int, int], zoom: float,
                center: tuple[float, float],
                cancelled: Callable[[], bool] | None = None) -> PreparedImage: ...


def _preview_source(source: str | Path | PreviewSource | None):
    return source if source is None or isinstance(source, PreviewSource) else Path(source)


_capabilities = ImageCapabilities()
_native_widgets: dict[str, Any] = {}
_preview_widget_class: Any = None


def get_capabilities() -> ImageCapabilities:
    """Return the cached result without querying stdin (safe inside Textual)."""
    return _capabilities


def _is_terminal() -> bool:
    return bool(
        sys.__stdout__ and sys.__stdin__
        and sys.__stdout__.isatty() and sys.__stdin__.isatty()
    )


def _kitty_widget(kitty: Any, widgets: Any) -> Any:
    """Keep terminal cleanup scoped to one preview with textual-image 0.14.x."""
    class ScopedKittyRenderable(kitty.Image):
        def cleanup(self) -> None:
            if self.terminal_image_id is not None:
                # 0.14.1 sends a=d,I=id, but its uploads use image ID i. Kitty's
                # d=I selects that ID and frees its pixels; default d=a can
                # affect other placements. Keep the dependency's tmux wrapper.
                # https://sw.kovidgoyal.net/kitty/graphics-protocol/#deleting-images
                kitty._send_tgp_message(a="d", d="I", i=self.terminal_image_id, q=2)
                self.terminal_image_id = None

    class ScopedKittyImage(widgets.TGPImage, Renderable=ScopedKittyRenderable):
        pass

    return ScopedKittyImage


def initialize_images(mode: str = "auto") -> ImageCapabilities:
    """Probe protocol support once, before Textual starts its input reader.

    Explicit protocols still require a positive probe. A terminal name alone is
    not proof of support, particularly over SSH or inside tmux. A reported
    protocol is capability detection, not a claim that DDH tested this terminal.
    """
    global _capabilities
    if mode not in {"auto", "sixel", "kitty", "off"}:
        raise ValueError("Image renderer must be auto, sixel, kitty, or off")
    if mode == "off":
        _capabilities = ImageCapabilities(message="Inline images disabled. Open original to inspect.")
        return _capabilities
    if not _is_terminal():
        _capabilities = ImageCapabilities(message="No interactive image terminal. Open original to inspect.")
        return _capabilities
    if sys.version_info < (3, 12):
        _capabilities = ImageCapabilities(message="Terminal images require Python 3.12+ and ddh[tui].")
        return _capabilities
    try:
        # renderable imports run textual-image's batched, cached protocol probe;
        # widget import also primes its cell-size cache before App owns stdin.
        sixel = importlib.import_module("textual_image.renderable.sixel")
        kitty = importlib.import_module("textual_image.renderable.tgp")
        widgets = importlib.import_module("textual_image.widget")
        terminal = importlib.import_module("textual_image._terminal")
        has_sixel = sixel.query_terminal_support()
        has_kitty = kitty.query_terminal_support()
        cell = terminal.get_cell_size()
        renderer = "off"
        if mode in {"auto", "sixel"} and has_sixel:
            renderer = "sixel"
        elif mode in {"auto", "kitty"} and has_kitty:
            renderer = "kitty"
        if renderer == "sixel":
            _native_widgets[renderer] = widgets.SixelImage
        elif renderer == "kitty":
            _native_widgets[renderer] = _kitty_widget(kitty, widgets)
        if renderer == "off":
            message = f"{'Requested ' + mode + ' graphics' if mode != 'auto' else 'Native image graphics'} unavailable. Open original to inspect."
        else:
            message = f"{renderer.capitalize()} images detected"
            if os.environ.get("TMUX"):
                message += " through tmux; use Open original if rendering is incomplete"
        _capabilities = ImageCapabilities(renderer, message, max(1, cell.width), max(1, cell.height))
    except (ImportError, OSError, RuntimeError, ValueError, TimeoutError) as exc:
        _capabilities = ImageCapabilities(message=f"Image renderer unavailable ({type(exc).__name__}). Open original to inspect.")
    return _capabilities


def _signature(path: Path) -> tuple:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise PreviewError("Preview requires a regular image file")
    return str(path), info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev


class ImagePreviewCache:
    """Thread-safe LRU of rendered thumbnails, with bounded source decoding.

    Only one source is decoded at a time. Original full-size images are never
    cached. Crops are taken from the oriented original before downsampling, so
    zooming reveals actual detail rather than enlarging a thumbnail.
    """

    def __init__(self, *, max_bytes: int = 32 * 1024 * 1024, max_entries: int = 24,
                 max_source_bytes: int = 128 * 1024 * 1024, max_pixels: int = 64_000_000):
        if min(max_bytes, max_entries, max_source_bytes, max_pixels) <= 0:
            raise ValueError("Preview limits must be positive")
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.max_source_bytes = max_source_bytes
        self.max_pixels = max_pixels
        self._entries: OrderedDict[tuple, PreparedImage] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    @property
    def cached_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            for entry in self._entries.values():
                entry.image.close()
            self._entries.clear()
            self._bytes = 0

    @staticmethod
    def _cost(result: PreparedImage) -> int:
        return result.image.width * result.image.height * len(result.image.getbands())

    def prepare(self, path: str | Path, *, max_size: tuple[int, int] = (1024, 768),
                zoom: float = 1.0, center: tuple[float, float] = (0.5, 0.5),
                cancelled: Callable[[], bool] | None = None) -> PreparedImage:
        """Return owned preview pixels; callers may close or alter their copy."""
        if len(max_size) != 2 or any(not isinstance(side, int) or side < 1 or side > 2048 for side in max_size):
            raise ValueError("Preview size must be 1–2048 pixels on each side")
        if not math.isfinite(zoom) or not 1.0 <= zoom <= 16.0:
            raise ValueError("Preview zoom must be between 1 and 16")
        if len(center) != 2 or any(not math.isfinite(value) or not 0 <= value <= 1 for value in center):
            raise ValueError("Preview center must be normalized coordinates from 0 to 1")
        max_size = tuple(max_size)
        center = tuple(center)
        try:
            source = Path(path).expanduser().resolve(strict=True)
            signature = _signature(source)
            if signature[1] > self.max_source_bytes:
                raise PreviewError("Image exceeds the preview file-size limit. Open original to inspect.")
            key = (*signature, max_size, zoom, center)
            with self._lock:
                if cancelled is not None and cancelled():
                    raise PreviewError("Preview cancelled")
                if key in self._entries:
                    result = self._entries.pop(key)
                    self._entries[key] = result
                    return replace(result, image=result.image.copy())
                result = self._decode(source, signature, max_size, zoom, center)
                if cancelled is not None and cancelled():
                    result.image.close()
                    raise PreviewError("Preview cancelled")
                cost = self._cost(result)
                # A replaced source must not occupy the cache under several old
                # signatures, even when navigating among different crop sizes.
                for old_key in list(self._entries):
                    if old_key[0] == signature[0] and old_key[:len(signature)] != signature:
                        old = self._entries.pop(old_key)
                        self._bytes -= self._cost(old)
                        old.image.close()
                while self._entries and (self._bytes + cost > self.max_bytes or len(self._entries) >= self.max_entries):
                    _, old = self._entries.popitem(last=False)
                    self._bytes -= self._cost(old)
                    old.image.close()
                if cost <= self.max_bytes:
                    self._entries[key] = replace(result, image=result.image.copy())
                    self._bytes += cost
                return result
        except PreviewError:
            raise
        except (OSError, ValueError, EOFError, SyntaxError, MemoryError) as exc:
            raise PreviewError(f"Cannot preview {Path(path).name}: {exc}") from exc

    def _decode(self, source: Path, signature: tuple, max_size: tuple[int, int],
                zoom: float, center: tuple[float, float]) -> PreparedImage:
        from PIL import Image, ImageOps, UnidentifiedImageError

        # O_NONBLOCK prevents a path replaced by a FIFO between stat/open from
        # blocking a preview worker. fstat checks the actual opened object.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(source, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            opened = (str(source), info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev)
            # Python 3.12 on Windows exposes different ctime semantics through
            # stat and fstat. Identity, size, and mtime are comparable on both.
            comparable = (0, 1, 2, 4, 5)
            if not stat.S_ISREG(info.st_mode) or any(opened[index] != signature[index] for index in comparable):
                raise PreviewError("Image changed while opening it; select it again to refresh")
            try:
                with Image.open(stream) as original:
                    if original.width * original.height > self.max_pixels:
                        raise PreviewError("Image exceeds the preview pixel limit. Open original to inspect.")
                    image_format = original.format or "image"
                    is_animated = bool(getattr(original, "is_animated", False))
                    original.seek(0)  # Never decode an entire animation.
                    with ImageOps.exif_transpose(original) as oriented:
                        width, height = oriented.size
                        crop_width = max(1, round(width / zoom))
                        crop_height = max(1, round(height / zoom))
                        left = max(0, min(width - crop_width, round(width * center[0] - crop_width / 2)))
                        top = max(0, min(height - crop_height, round(height * center[1] - crop_height / 2)))
                        box = (left, top, left + crop_width, top + crop_height)
                        with oriented.crop(box) as crop:
                            crop.thumbnail(max_size, Image.Resampling.LANCZOS)
                            pixels = crop.convert("RGBA")
            except (Image.DecompressionBombError, UnidentifiedImageError) as exc:
                raise PreviewError("Unsupported or oversized image. Open original to inspect.") from exc
            try:
                unchanged = _signature(source) == signature
            except (OSError, PreviewError):
                pixels.close()
                raise
            if not unchanged:
                pixels.close()
                raise PreviewError("Image changed during decoding; select it again to refresh")
        return PreparedImage(pixels, source, (width, height), box, zoom, image_format,
                             is_animated, signature)


_cache = ImagePreviewCache()


def prepare_image(path: str | Path | PreviewSource, *, max_size: tuple[int, int] = (1024, 768),
                  zoom: float = 1.0, center: tuple[float, float] = (0.5, 0.5),
                  cancelled: Callable[[], bool] | None = None) -> PreparedImage:
    if isinstance(path, PreviewSource):
        return path.prepare(max_size=max_size, zoom=zoom, center=center, cancelled=cancelled)
    return _cache.prepare(path, max_size=max_size, zoom=zoom, center=center, cancelled=cancelled)


def open_external(path: str | Path) -> None:
    """Open an existing regular file with its desktop viewer, without a shell."""
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise OSError("Open original requires a regular file")
    if sys.platform == "win32":
        os.startfile(str(source))
    else:
        command = ["open", "--", str(source)] if sys.platform == "darwin" else ["xdg-open", str(source)]
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def create_image_preview(path: str | Path | PreviewSource | None, *, id: str | None = None,
                         capability: ImageCapabilities | None = None, zoom: float = 1.0) -> Any:
    """Create a worker-backed Textual widget without probing terminal input.

    The returned widget has set_source(path) and set_zoom(zoom, center=(x, y)).
    Repeating an unchanged source preserves zoom and rendered pixels; a changed
    file identity invalidates the preview even when the path stays the same.
    Equal normalized centers/zoom on two widgets provide linked comparisons.
    """
    global _preview_widget_class
    if _preview_widget_class is None:
        from textual import work
        from textual.containers import Vertical
        from textual.widgets import Static
        from textual.worker import get_current_worker

        class ImagePreview(Vertical):
            DEFAULT_CSS = """
            ImagePreview { height: 14; min-height: 5; overflow: hidden; }
            ImagePreview > .ddh-image-pixels { width: auto; height: auto; max-width: 100%; max-height: 100%; }
            ImagePreview > .ddh-image-message { height: auto; color: $text-muted; }
            """

            def __init__(self, source, *, id=None, capability=None, zoom=1.0):
                super().__init__(id=id)
                self.source = _preview_source(source)
                self.capability = capability or get_capabilities()
                self.zoom = zoom
                self.center = (0.5, 0.5)
                self._ticket = 0
                self._request_key = None
                self._active_worker = None
                self._resize_timer = None
                self._owned_pixels = None
                self._retired_pixels = []
                self.prepared = None
                self.original_size = None
                native = _native_widgets.get(self.capability.renderer)
                if native is None and self.capability.renderer != "off":
                    self.capability = ImageCapabilities()
                try:
                    self._image_widget = native(classes="ddh-image-pixels", on_error=self._render_error) if native else None
                except (ImportError, OSError, ValueError, RuntimeError):
                    native = None
                    self._image_widget = None
                    self.capability = ImageCapabilities(message="Image renderer unavailable. Open original to inspect.")
                initial = ("Select an image to preview." if self.source is None else
                           "Loading preview…" if native else self.capability.message)
                self._message = Static(initial, classes="ddh-image-message", markup=False)

            def compose(self):
                yield self._message
                if self._image_widget is not None:
                    yield self._image_widget

            def _render_error(self, exc):
                return Static("Image rendering failed. Open original to inspect.", markup=False)

            def on_mount(self):
                self.call_after_refresh(self._request_preview)

            def on_resize(self):
                if self._resize_timer:
                    self._resize_timer.stop()
                self._resize_timer = self.set_timer(0.12, self._request_preview)

            def on_unmount(self):
                self._ticket += 1
                if self._resize_timer:
                    self._resize_timer.stop()
                if self._active_worker:
                    self._active_worker.cancel()
                self._clear_image(defer=False)
                self._release_retired_pixels()

            def _release_retired_pixels(self):
                for image in self._retired_pixels:
                    image.close()
                self._retired_pixels.clear()

            def _clear_image(self, *, defer=True):
                if self._image_widget is not None:
                    self._image_widget.image = None
                if self._owned_pixels is not None:
                    self._retired_pixels.append(self._owned_pixels)
                    self._owned_pixels = None
                    # Sixel replaces an internal child asynchronously. Release
                    # pixels only after that child has finished recomposing.
                    if defer:
                        self.call_after_refresh(self._release_retired_pixels)
                self.prepared = None
                self.original_size = None

            def set_source(self, source):
                source = _preview_source(source)
                if source != self.source:
                    self.source = source
                    self.zoom = 1.0
                    self.center = (0.5, 0.5)
                self._request_preview()

            def set_zoom(self, zoom, center=(0.5, 0.5)):
                self.zoom = zoom
                self.center = center
                self._request_preview()

            def _request_preview(self):
                if not self.is_mounted:
                    return
                size = (max(1, min(1536, self.content_size.width * self.capability.cell_width)),
                        max(1, min(1024, (self.content_size.height - 1) * self.capability.cell_height)))
                signature = None
                if self.source is not None and self._image_widget is not None:
                    try:
                        signature = (self.source if isinstance(self.source, PreviewSource)
                                     else _signature(self.source.expanduser().resolve(strict=True)))
                    except (OSError, PreviewError) as exc:
                        # Keep errors in the key too: identical polling updates
                        # should not clear and re-render the same fallback.
                        signature = (str(self.source), type(exc).__name__, str(exc))
                key = (self.source, signature, size, self.zoom, self.center)
                if key == self._request_key:
                    return
                previous_key, self._request_key = self._request_key, key
                self._ticket += 1
                if self._active_worker:
                    self._active_worker.cancel()
                # A new source must never display the preceding file. Retain
                # pixels while resizing or zooming the same unchanged source,
                # then replace them once the worker has a complete new frame.
                if previous_key is None or previous_key[:2] != key[:2]:
                    self._clear_image()
                self._message.display = True
                if self.source is None:
                    self._message.update("Select an image to preview.")
                elif self._image_widget is None:
                    self._message.update(self.capability.message)
                else:
                    self._message.update("Loading preview…")
                    self._active_worker = self._load(self._ticket, self.source, size, self.zoom, self.center)

            @work(thread=True, exclusive=True, group="image-preview", exit_on_error=False)
            def _load(self, ticket, source, size, zoom, center):
                worker = get_current_worker()
                if worker.is_cancelled:
                    return
                try:
                    prepared = prepare_image(source, max_size=size, zoom=zoom, center=center,
                                             cancelled=lambda: worker.is_cancelled)
                except (PreviewError, ValueError) as exc:
                    if not worker.is_cancelled:
                        try:
                            self.app.call_from_thread(self._finish, ticket, None, str(exc))
                        except RuntimeError:
                            pass  # App closed before the decode error arrived.
                    return
                if worker.is_cancelled:
                    prepared.image.close()
                else:
                    try:
                        self.app.call_from_thread(self._finish, ticket, prepared, None)
                    except RuntimeError:  # App closed while the worker decoded.
                        prepared.image.close()

            def _finish(self, ticket, prepared, error):
                if ticket != self._ticket or not self.is_mounted:
                    if prepared:
                        prepared.image.close()
                    return
                if error:
                    self._clear_image()
                    self._message.update(error)
                    self._message.display = True
                else:
                    try:
                        old_pixels = self._owned_pixels
                        self._owned_pixels = prepared.image
                        if old_pixels is not None:
                            self._retired_pixels.append(old_pixels)
                            self.call_after_refresh(self._release_retired_pixels)
                        self._image_widget.image = prepared.image
                    except (OSError, ValueError, RuntimeError):
                        self._clear_image()
                        self._message.update("Image rendering failed. Open original to inspect.")
                        self._message.display = True
                    else:
                        self.prepared = prepared
                        self.original_size = prepared.original_size
                        self._message.update("Animated image · first frame only" if prepared.is_animated else "")
                        self._message.display = prepared.is_animated

        _preview_widget_class = ImagePreview
    return _preview_widget_class(path, id=id, capability=capability, zoom=zoom)

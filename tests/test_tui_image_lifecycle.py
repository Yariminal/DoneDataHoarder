"""Actual Textual event-loop checks; these do not qualify a native terminal."""
import asyncio
import io
import os
import sys
import threading

import pytest
from PIL import Image

pytest.importorskip("textual")
from textual.app import App
from textual.widgets import Static

from donedatahoarder.tui import images


def save_image(path, color="red", size=(128, 64), *, compress_level=6):
    with Image.new("RGB", size, color) as source:
        source.save(path, format="PNG", compress_level=compress_level)
    return path


class TrackedNative(Static):
    """Record terminal image replacements without pretending to render pixels."""
    def __init__(self, **kwargs):
        kwargs.pop("on_error", None)
        super().__init__("", **kwargs)
        self.assignments = []
        self._image = None

    @property
    def image(self):
        return self._image

    @image.setter
    def image(self, value):
        self._image = value
        self.assignments.append(value)


class PreviewApp(App):
    def compose(self):
        yield images.create_image_preview(None, id="preview", capability=images.ImageCapabilities("sixel"))


async def settle(app, pilot):
    await app.workers.wait_for_complete([worker for worker in app.workers if not worker.is_cancelled])
    await pilot.pause(0.2)


def test_unchanged_snapshot_does_not_replace_pixels_or_reset_zoom(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "_native_widgets", {"sixel": TrackedNative})
    # Uncompressed PNG blocks have equal lengths for equal dimensions on all
    # zlib builds; color must change without changing size or mtime.
    path = save_image(tmp_path / "selected.png", compress_level=0)

    async def exercise():
        app = PreviewApp()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            widget = app.query_one("#preview")
            widget.set_source(path)
            await settle(app, pilot)
            widget.set_zoom(2, center=(0.75, 0.5))
            await settle(app, pilot)
            native = widget._image_widget
            pixels = native.image
            assignments = len(native.assignments)
            ticket = widget._ticket
            for _ in range(10):
                widget.set_source(path)
                widget.set_zoom(2, center=(0.75, 0.5))
            await settle(app, pilot)
            assert widget.zoom == 2
            assert widget.center == (0.75, 0.5)
            assert widget.prepared.crop_box == (64, 16, 128, 48)
            assert widget._ticket == ticket
            assert native.image is pixels
            assert len(native.assignments) == assignments

            # A replacement with identical size and mtime must still refresh.
            before = path.stat()
            replacement = save_image(tmp_path / "replacement.png", "blue", compress_level=0)
            assert replacement.stat().st_size == before.st_size
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(replacement, path)
            widget.set_source(path)
            assert native.image is None  # Never show obsolete source pixels.
            await settle(app, pilot)
            assert native.image.getpixel((0, 0)) == (0, 0, 255, 255)
            with pytest.raises(ValueError, match="closed"):
                pixels.getpixel((0, 0))

    asyncio.run(exercise())


def test_resize_retains_pixels_until_ready_and_clear_cancels_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "_native_widgets", {"sixel": TrackedNative})
    path = save_image(tmp_path / "selected.png", size=(1000, 500))
    ready = threading.Event()
    release = threading.Event()
    block = threading.Event()
    cache = images.ImagePreviewCache()
    decoded = []

    def controlled_prepare(path, **kwargs):
        result = cache.prepare(path, **kwargs)
        decoded.append(result)
        if block.is_set():
            ready.set()
            release.wait(3)
        return result

    monkeypatch.setattr(images, "prepare_image", controlled_prepare)

    async def exercise():
        app = PreviewApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.2)
            widget = app.query_one("#preview")
            widget.set_source(path)
            await settle(app, pilot)
            old = widget._image_widget.image
            block.set()
            await pilot.resize_terminal(60, 30)
            assert await asyncio.to_thread(ready.wait, 2)
            assert widget._image_widget.image is old
            assert old.getpixel((0, 0)) == (255, 0, 0, 255)
            pending_worker = widget._active_worker
            widget.set_source(None)
            assert pending_worker.is_cancelled
            assert widget._image_widget.image is None
            release.set()
            await pilot.pause(0.3)
            assert widget.prepared is None
            assert "Select an image" in str(widget._message.render())
        for result in decoded:
            with pytest.raises(ValueError, match="closed"):
                result.image.getpixel((0, 0))

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_unmount_cancels_pending_decode_and_releases_late_result(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "_native_widgets", {"sixel": TrackedNative})
    path = save_image(tmp_path / "selected.png")
    ready = threading.Event()
    release = threading.Event()
    decoded = []

    def controlled_prepare(path, **kwargs):
        result = images.ImagePreviewCache().prepare(path, **kwargs)
        decoded.append(result)
        ready.set()
        release.wait(3)
        return result

    monkeypatch.setattr(images, "prepare_image", controlled_prepare)

    async def exercise():
        app = PreviewApp()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            preview = app.query_one("#preview")
            preview.set_source(path)
            assert await asyncio.to_thread(ready.wait, 2)
            worker = preview._active_worker
            await preview.remove()
            assert worker.is_cancelled
            release.set()
            await pilot.pause(0.2)
            assert preview.prepared is None
            assert preview._owned_pixels is None
        with pytest.raises(ValueError, match="closed"):
            decoded[0].image.getpixel((0, 0))

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_animation_disclosure_remains_visible_in_widget(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "_native_widgets", {"sixel": TrackedNative})
    path = tmp_path / "animated.gif"
    with Image.new("RGB", (32, 16), "red") as first, Image.new("RGB", (32, 16), "blue") as second:
        first.save(path, save_all=True, append_images=[second], duration=100, loop=0)

    async def exercise():
        app = PreviewApp()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            widget = app.query_one("#preview")
            widget.set_source(path)
            await settle(app, pilot)
            assert widget._message.display
            assert "first frame only" in str(widget._message.render())
            assert widget.prepared.is_animated
            assert widget._image_widget.image.getpixel((0, 0)) == (255, 0, 0, 255)

    asyncio.run(exercise())


@pytest.mark.parametrize("renderer", ["sixel", "kitty"])
def test_real_native_widgets_survive_replace_resize_and_unmount(tmp_path, monkeypatch, renderer):
    pytest.importorskip("textual_image")
    from textual_image import widget as native_widgets
    from textual_image.renderable import tgp

    native = native_widgets.SixelImage if renderer == "sixel" else images._kitty_widget(tgp, native_widgets)
    monkeypatch.setattr(images, "_native_widgets", {"sixel": native})
    monkeypatch.setattr(sys, "__stdout__", io.StringIO())
    red = save_image(tmp_path / "red.png")
    blue = save_image(tmp_path / "blue.png", "blue")

    async def exercise():
        app = PreviewApp()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            preview = app.query_one("#preview")
            preview.set_source(red)
            await settle(app, pilot)
            old = preview._owned_pixels
            preview.set_zoom(2)
            await settle(app, pilot)
            assert preview.prepared.crop_box == (32, 16, 96, 48)
            with pytest.raises(ValueError, match="closed"):
                old.getpixel((0, 0))
            preview.set_source(blue)
            await settle(app, pilot)
            await pilot.resize_terminal(60, 30)
            await settle(app, pilot)
            current = preview._owned_pixels
            assert current.getpixel((0, 0)) == (0, 0, 255, 255)
        with pytest.raises(ValueError, match="closed"):
            current.getpixel((0, 0))

    asyncio.run(exercise())

"""Image bounds, original-pixel crops, source invalidation, and renderer fallback."""
import asyncio
import io
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from donedatahoarder.tui import images


def save_image(path, color="red", size=(64, 32)):
    with Image.new("RGB", size, color) as image:
        image.save(path, format="PNG")
    return path


def test_exif_orientation_is_applied_before_sizing(tmp_path):
    path = tmp_path / "rotated.jpg"
    with Image.new("RGB", (40, 20), "red") as source:
        exif = source.getexif()
        exif[274] = 6
        source.save(path, exif=exif)
    result = images.ImagePreviewCache().prepare(path, max_size=(100, 100))
    assert result.original_size == (20, 40)
    assert result.image.size == (20, 40)
    assert result.crop_box == (0, 0, 20, 40)


def test_zoom_crops_original_pixels_before_downsampling(tmp_path):
    path = tmp_path / "details.png"
    with Image.new("RGB", (160, 80)) as source:
        source.putdata([(x, y, 0) for y in range(80) for x in range(160)])
        source.save(path)
    cache = images.ImagePreviewCache()
    thumbnail = cache.prepare(path, max_size=(16, 8))
    result = cache.prepare(path, max_size=(80, 40), zoom=2)
    assert thumbnail.image.size == (16, 8)
    assert result.crop_box == (40, 20, 120, 60)
    assert result.image.size == (80, 40)
    assert result.image.getpixel((0, 0)) == (40, 20, 0, 255)
    corner = cache.prepare(path, max_size=(80, 40), zoom=2, center=(1, 1))
    assert corner.crop_box == (80, 40, 160, 80)


def test_cache_is_bounded_and_returns_owned_pixels(tmp_path):
    cache = images.ImagePreviewCache(max_bytes=64 * 32 * 4 * 2, max_entries=2)
    first = save_image(tmp_path / "first.png")
    result = cache.prepare(first)
    result.image.putpixel((0, 0), (0, 0, 0, 0))
    result.image.close()
    assert cache.prepare(first).image.getpixel((0, 0)) == (255, 0, 0, 255)
    for index in range(4):
        cache.prepare(save_image(tmp_path / f"{index}.png"))
    assert cache.entry_count == 2
    assert cache.cached_bytes <= cache.max_bytes
    cache.clear()
    assert cache.cached_bytes == cache.entry_count == 0


def test_cache_invalidates_when_source_changes(tmp_path):
    cache = images.ImagePreviewCache()
    path = save_image(tmp_path / "changing.png")
    assert cache.prepare(path).image.getpixel((0, 0))[0] == 255
    old_time = path.stat().st_mtime_ns
    save_image(path, "blue")
    os.utime(path, ns=(old_time + 10_000_000, old_time + 10_000_000))
    assert cache.prepare(path).image.getpixel((0, 0)) == (0, 0, 255, 255)
    assert cache.entry_count == 1


def test_cancelled_decode_does_not_keep_pixels_or_fill_cache(tmp_path, monkeypatch):
    cache = images.ImagePreviewCache()
    decode = cache._decode
    cancelled = threading.Event()
    decoded = []

    def cancel_after_decode(*args):
        result = decode(*args)
        decoded.append(result)
        cancelled.set()
        return result

    monkeypatch.setattr(cache, "_decode", cancel_after_decode)
    with pytest.raises(images.PreviewError, match="cancelled"):
        cache.prepare(save_image(tmp_path / "image.png"), cancelled=cancelled.is_set)
    assert cache.entry_count == cache.cached_bytes == 0
    with pytest.raises(ValueError, match="closed"):
        decoded[0].image.getpixel((0, 0))


def test_animation_preview_uses_and_discloses_only_first_frame(tmp_path):
    path = tmp_path / "animated.gif"
    with Image.new("RGB", (32, 16), "red") as first, Image.new("RGB", (32, 16), "blue") as second:
        first.save(path, save_all=True, append_images=[second], duration=100, loop=0)
    result = images.ImagePreviewCache().prepare(path)
    try:
        assert result.is_animated
        assert result.image.getpixel((0, 0)) == (255, 0, 0, 255)
    finally:
        result.image.close()


def test_source_byte_and_pixel_limits_and_corrupt_images(tmp_path):
    path = save_image(tmp_path / "bounded.png")
    with pytest.raises(images.PreviewError, match="file-size limit"):
        images.ImagePreviewCache(max_source_bytes=1).prepare(path)
    with pytest.raises(images.PreviewError, match="pixel limit"):
        images.ImagePreviewCache(max_pixels=10).prepare(path)
    corrupt = tmp_path / "broken.jpg"
    corrupt.write_bytes(b"this is not an image")
    with pytest.raises(images.PreviewError, match="Unsupported"):
        images.prepare_image(corrupt)
    with pytest.raises(images.PreviewError, match="regular"):
        images.prepare_image(tmp_path)


@pytest.mark.parametrize("kwargs", [{"zoom": 0}, {"zoom": float("nan")}, {"max_size": (4096, 10)}, {"center": (2, 0)}])
def test_invalid_preview_geometry_is_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):
        images.prepare_image(save_image(tmp_path / "image.png"), **kwargs)


def test_disabled_or_noninteractive_mode_never_imports_or_probes(monkeypatch):
    monkeypatch.setattr(images, "_capabilities", images.ImageCapabilities())
    monkeypatch.setattr(images.importlib, "import_module", lambda name: pytest.fail("must not probe"))
    assert images.initialize_images("off").renderer == "off"
    monkeypatch.setattr(images, "_is_terminal", lambda: False)
    assert images.initialize_images("auto").renderer == "off"
    assert images.get_capabilities().renderer == "off"


def test_protocol_detection_honors_explicit_request(monkeypatch):
    monkeypatch.setattr(images, "_is_terminal", lambda: True)
    monkeypatch.setattr(images.sys, "version_info", (3, 12, 3))
    monkeypatch.setattr(images, "_capabilities", images.ImageCapabilities())
    monkeypatch.setattr(images, "_native_widgets", {})
    modules = {
        "textual_image.renderable.sixel": SimpleNamespace(query_terminal_support=lambda: True),
        "textual_image.renderable.tgp": SimpleNamespace(query_terminal_support=lambda: False),
        "textual_image.widget": SimpleNamespace(SixelImage=object, TGPImage=object),
        "textual_image._terminal": SimpleNamespace(get_cell_size=lambda: SimpleNamespace(width=8, height=16)),
    }
    monkeypatch.setattr(images.importlib, "import_module", modules.__getitem__)
    assert images.initialize_images("auto").renderer == "sixel"
    assert images.get_capabilities().cell_width == 8
    assert images.initialize_images("kitty").renderer == "off"
    with pytest.raises(ValueError):
        images.initialize_images("something-else")


def test_missing_native_dependency_falls_back_without_crashing(monkeypatch):
    monkeypatch.setattr(images, "_is_terminal", lambda: True)
    monkeypatch.setattr(images.sys, "version_info", (3, 12, 3))
    monkeypatch.setattr(images, "_capabilities", images.ImageCapabilities())

    def missing_module(name):
        raise ImportError("textual-image is missing")

    monkeypatch.setattr(images.importlib, "import_module", missing_module)
    capability = images.initialize_images("auto")
    assert capability.renderer == "off"
    assert "Open original" in capability.message


@pytest.mark.parametrize("tmux", [False, True])
def test_kitty_cleanup_only_frees_its_own_image(monkeypatch, tmux):
    pytest.importorskip("textual_image")
    from textual_image.renderable import tgp
    from textual_image import widget
    from textual_image._terminal import prepare_terminal_sequence

    if tmux:
        monkeypatch.setenv("TMUX", "/tmp/tmux-test/default,1,0")
    else:
        monkeypatch.delenv("TMUX", raising=False)
    output = io.StringIO()
    monkeypatch.setattr(sys, "__stdout__", output)
    native = images._kitty_widget(tgp, widget)
    with Image.new("RGB", (2, 2), "red") as source:
        first = native._Renderable(source)
        second = native._Renderable(source)
    first._send_image_to_terminal(2, 2)
    second._send_image_to_terminal(2, 2)
    first_id, second_id = first.terminal_image_id, second.terminal_image_id
    assert first_id != second_id
    output.seek(0)
    output.truncate()

    first.cleanup()
    first.cleanup()  # Closing or replacing twice must not delete anything else.
    assert output.getvalue() == prepare_terminal_sequence(f"\x1b_Ga=d,d=I,i={first_id},q=2\x1b\\")
    assert first.terminal_image_id is None
    assert second.terminal_image_id == second_id
    second.cleanup()
    assert output.getvalue().endswith(prepare_terminal_sequence(f"\x1b_Ga=d,d=I,i={second_id},q=2\x1b\\"))


def test_external_open_uses_an_argument_array_not_a_shell(tmp_path, monkeypatch):
    path = save_image(tmp_path / "name ; $(not-a-command).png")
    calls = []
    monkeypatch.setattr(images.sys, "platform", "linux")
    monkeypatch.setattr(images.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))
    images.open_external(path)
    assert calls[0][0] == (["xdg-open", str(path.resolve())],)
    assert not calls[0][1].get("shell", False)
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    with pytest.raises(OSError):
        images.open_external(tmp_path / "missing.png")


def test_module_import_does_not_load_terminal_libraries():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import donedatahoarder.tui.images; import donedatahoarder.tui.theme; assert 'textual_image' not in sys.modules; assert 'textual' not in sys.modules"],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_preview_widget_has_headless_fallback_without_probe(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from textual.app import App
    from textual.widgets import Static

    monkeypatch.setattr(images, "_capabilities", images.ImageCapabilities(message="Preview unavailable. Open original."))
    monkeypatch.setattr(images, "_native_widgets", {})
    monkeypatch.setattr(images, "initialize_images", lambda *args: pytest.fail("Widget must not probe"))
    path = save_image(tmp_path / "image.png")

    class PreviewApp(App):
        def compose(self):
            yield images.create_image_preview(path, id="preview")

    async def exercise():
        app = PreviewApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            widget = app.query_one("#preview")
            assert "Preview unavailable" in str(widget.query_one(Static).render())
            widget.set_source(None)
            await pilot.pause()
            assert "Select an image" in str(widget.query_one(Static).render())

    asyncio.run(exercise())


def test_preview_workers_discard_stale_images_and_release_pixels(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from textual.app import App
    from textual.widgets import Static

    class FakeNativeImage(Static):
        def __init__(self, **kwargs):
            kwargs.pop("on_error", None)
            super().__init__("", **kwargs)
            self.image = None

    monkeypatch.setattr(images, "_native_widgets", {"sixel": FakeNativeImage})
    red = save_image(tmp_path / "red.png")
    blue = save_image(tmp_path / "blue.png", "blue")
    ready = threading.Event()
    release = threading.Event()
    prepared_results = []
    cache = images.ImagePreviewCache()

    def controlled_prepare(path, **kwargs):
        result = cache.prepare(path, **kwargs)
        prepared_results.append(result)
        if path == red:
            ready.set()
            release.wait(2)
        return result

    monkeypatch.setattr(images, "prepare_image", controlled_prepare)

    class PreviewApp(App):
        def compose(self):
            yield images.create_image_preview(None, id="preview", capability=images.ImageCapabilities("sixel"))

    async def exercise():
        app = PreviewApp()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)  # Allow the initial resize debounce to settle.
            widget = app.query_one("#preview")
            widget.set_source(red)
            assert await asyncio.to_thread(ready.wait, 2)
            widget.set_source(blue)
            release.set()
            await app.workers.wait_for_complete([worker for worker in app.workers if not worker.is_cancelled])
            await pilot.pause()
            assert widget.original_size == (64, 32)
            assert widget._image_widget.image.getpixel((0, 0)) == (0, 0, 255, 255)
            old = widget._image_widget.image
            widget.set_zoom(2)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert widget.prepared.crop_box == (16, 8, 48, 24)
            with pytest.raises(ValueError, match="closed"):
                old.getpixel((0, 0))
            current = widget._image_widget.image
        with pytest.raises(ValueError, match="closed"):
            current.getpixel((0, 0))
        for result in prepared_results:
            with pytest.raises(ValueError, match="closed"):
                result.image.getpixel((0, 0))

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_native_preview_reports_decode_failure(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from textual.app import App
    from textual.widgets import Static

    class FakeNativeImage(Static):
        def __init__(self, **kwargs):
            kwargs.pop("on_error", None)
            super().__init__("", **kwargs)
            self.image = None

    monkeypatch.setattr(images, "_native_widgets", {"sixel": FakeNativeImage})
    corrupt = tmp_path / "broken.png"
    corrupt.write_bytes(b"not PNG data")

    class PreviewApp(App):
        def compose(self):
            yield images.create_image_preview(None, id="preview", capability=images.ImageCapabilities("sixel"))

    async def exercise():
        app = PreviewApp()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            widget = app.query_one("#preview")
            widget.set_source(corrupt)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "Open original" in str(widget._message.render())
            assert widget._message.display
            assert widget._image_widget.image is None
            assert widget.original_size is None

    asyncio.run(exercise())

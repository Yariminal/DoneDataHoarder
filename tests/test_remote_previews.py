"""Workstation image boundaries and local terminal preview source lifecycle."""
import asyncio
import base64
import io
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from donedatahoarder.remote import previews
from donedatahoarder.tui import images


def save_image(path, color="red", size=(160, 80)):
    with Image.new("RGB", size, color) as image:
        image.save(path)
    return path


def payload(size=(16, 8), color="red", **overrides):
    with Image.new("RGBA", size, color) as image, io.BytesIO() as output:
        image.save(output, format="PNG")
        result = {"image": base64.b64encode(output.getvalue()).decode("ascii"),
                  "original_size": [160, 80], "crop_box": [0, 0, 160, 80],
                  "format": "PNG", "revision": "live-revision", "is_animated": False}
    return {**result, **overrides}


class Connection:
    generation = 0

    def __init__(self, response=None):
        self.response = response or payload()
        self.calls = []

    def request_json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.response


@pytest.fixture
def preview_app(tmp_path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    root = tmp_path / "collection"
    root.mkdir()
    source = save_image(root / "image.png")
    file = {"id": 4, "session_id": "session-a", "path": str(source),
            "root_path": str(root), "mime_type": "image/png"}
    app = fastapi.FastAPI()

    def authorize(session_id, file_id):
        if session_id != "session-a" or file_id != 4:
            raise fastapi.HTTPException(404, "File not found in this session")
        return dict(file)

    app.state.remote_authorize_file = authorize
    previews.install_preview_route(app)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, file=file, root=root, source=source,
                              url="/remote/v1/sessions/session-a/files/4/preview")


def test_remote_source_only_sends_ids_and_bounded_geometry():
    connection = Connection()
    source = previews.remote_source(connection, "session-a", {
        "id": 42, "session_id": "session-a", "path": "C:\\private\\not-on-this-laptop.jpg",
        "preview_revision": "r1",
    })
    result = images.prepare_image(source, max_size=(32, 16), zoom=2, center=(1, 0))
    try:
        assert result.image.size == (16, 8)
        assert result.image.getpixel((0, 0)) == (255, 0, 0, 255)
        assert connection.calls == [("GET", "/sessions/session-a/files/42/preview", {
            "params": {"width": 32, "height": 16, "zoom": 2, "x": 1, "y": 0}})]
        assert result.path.name == "remote-preview-42"
    finally:
        result.image.close()


def test_source_identity_tracks_server_file_revision_and_reconnect():
    connection = Connection()
    file = {"id": 1, "preview_revision": "revision-1"}
    first = previews.remote_source(connection, "session-a", file)
    assert first == previews.remote_source(connection, "session-a", dict(file))
    assert hash(first) == hash(previews.remote_source(connection, "session-a", file))
    assert first != previews.remote_source(Connection(), "session-a", file)
    assert first != previews.remote_source(connection, "session-b", file)
    assert first != previews.remote_source(connection, "session-a", {**file, "preview_revision": "revision-2"})
    connection.generation += 1
    assert first != previews.remote_source(connection, "session-a", file)
    with pytest.raises(images.PreviewError, match="belong"):
        previews.remote_source(connection, "session-a", {"id": 1, "session_id": "session-b"})
    with pytest.raises(images.PreviewError, match="indexed"):
        previews.remote_source(connection, "session-a", {"id": "../../private"})


def test_server_zoom_crops_original_and_transfers_only_thumbnail(preview_app):
    with Image.new("RGB", (160, 80)) as image:
        image.putdata([(x, y, 0) for y in range(80) for x in range(160)])
        image.save(preview_app.source)
    response = preview_app.client.get(preview_app.url, params={
        "width": 80, "height": 40, "zoom": 2, "x": 1, "y": 1})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["original_size"] == [160, 80]
    assert data["crop_box"] == [80, 40, 160, 80]
    assert data["revision"] == previews.preview_revision(preview_app.file)
    with Image.open(io.BytesIO(base64.b64decode(data["image"]))) as image:
        assert image.format == "PNG"
        assert image.size == (80, 40)
        assert image.getpixel((0, 0)) == (80, 40, 0, 255)


def test_server_exif_orientation_and_animation_disclosure(preview_app):
    oriented_path = preview_app.root / "oriented.jpg"
    with Image.new("RGB", (40, 20), "red") as image:
        exif = image.getexif()
        exif[274] = 6
        image.save(oriented_path, exif=exif)
    preview_app.file["path"] = str(oriented_path)
    assert preview_app.client.get(preview_app.url).json()["original_size"] == [20, 40]
    animated_path = preview_app.root / "animated.gif"
    with Image.new("RGB", (20, 10), "red") as first, Image.new("RGB", (20, 10), "blue") as second:
        first.save(animated_path, save_all=True, append_images=[second], duration=100, loop=0)
    preview_app.file["path"] = str(animated_path)
    data = preview_app.client.get(preview_app.url).json()
    assert data["is_animated"]
    with Image.open(io.BytesIO(base64.b64decode(data["image"]))) as image:
        assert not getattr(image, "is_animated", False)
        assert image.getpixel((0, 0)) == (255, 0, 0, 255)


@pytest.mark.parametrize("params", [{"width": 2049}, {"height": 0}, {"zoom": 17},
                                     {"zoom": "nan"}, {"x": -0.1}, {"y": 1.1}])
def test_server_rejects_unbounded_geometry(preview_app, params):
    assert preview_app.client.get(preview_app.url, params=params).status_code == 422


def test_scope_rejects_cross_session_non_image_and_root_escape(preview_app, tmp_path):
    assert preview_app.client.get(preview_app.url.replace("session-a", "session-b")).status_code == 404
    assert preview_app.client.get(preview_app.url.replace("/4/", "/5/")).status_code == 404
    preview_app.file["mime_type"] = "application/pdf"
    assert preview_app.client.get(preview_app.url).status_code == 415
    preview_app.file["mime_type"] = "image/png"
    preview_app.file["path"] = str(save_image(tmp_path / "outside.png"))
    response = preview_app.client.get(preview_app.url)
    assert response.status_code == 422
    assert "outside" in response.json()["detail"]


def test_scope_rejects_linked_ancestry_even_inside_root(preview_app, monkeypatch):
    from donedatahoarder.core import scanner

    original = scanner._is_link_or_reparse
    monkeypatch.setattr(scanner, "_is_link_or_reparse", lambda path: path == preview_app.root or original(path))
    response = preview_app.client.get(preview_app.url)
    assert response.status_code == 422
    assert "symlink or junction" in response.json()["detail"]


def test_preview_source_replacement_between_authorization_and_decode_is_rejected(preview_app, monkeypatch):
    original = previews.prepare_image
    decoded = []

    def replace_before_prepare(path, **kwargs):
        save_image(path, "blue")
        os.utime(path, ns=(1_900_000_000_000_000_000, 1_900_000_000_000_000_000))
        result = original(path, **kwargs)
        decoded.append(result)
        return result

    monkeypatch.setattr(previews, "prepare_image", replace_before_prepare)
    response = preview_app.client.get(preview_app.url)
    assert response.status_code == 422
    assert "changed" in response.json()["detail"]
    with pytest.raises(ValueError, match="closed"):
        decoded[0].image.getpixel((0, 0))


def test_server_caps_png_transfer_and_closes_pixels(preview_app, monkeypatch):
    monkeypatch.setattr(previews, "MAX_PNG_BYTES", 10)
    response = preview_app.client.get(preview_app.url)
    assert response.status_code == 422
    assert "transfer limit" in response.json()["detail"]


@pytest.mark.parametrize("overrides", [
    {"image": "not base64"}, {"original_size": [100_000, 100_000]},
    {"original_size": [True, 20]}, {"crop_box": [0, 0, 200, 80]},
    {"crop_box": [10, 10, 1, 1]}, {"is_animated": "no"},
])
def test_client_rejects_malformed_preview_metadata(overrides):
    source = previews.remote_source(Connection(payload(**overrides)), "session-a", {"id": 1})
    with pytest.raises(images.PreviewError, match="Invalid workstation preview"):
        source.prepare()


def test_client_rejects_oversize_png_before_pixel_decode(monkeypatch):
    connection = Connection(payload(size=(100, 100)))
    source = previews.remote_source(connection, "session-a", {"id": 1})
    with pytest.raises(images.PreviewError, match="bounded"):
        source.prepare(max_size=(16, 16))
    monkeypatch.setattr(previews, "MAX_PNG_BYTES", 1)
    with pytest.raises(images.PreviewError, match="transfer limit"):
        source.prepare()


def test_cancelled_request_never_starts_or_decodes_and_late_pixels_are_closed(monkeypatch):
    connection = Connection()
    source = previews.remote_source(connection, "session-a", {"id": 1})
    with pytest.raises(images.PreviewError, match="cancelled"):
        source.prepare(cancelled=lambda: True)
    assert connection.calls == []

    checks = iter([False, True])
    monkeypatch.setattr(previews, "_decode_response", lambda *args, **kwargs: pytest.fail("cancelled response must not decode"))
    with pytest.raises(images.PreviewError, match="cancelled"):
        source.prepare(cancelled=lambda: next(checks))

    result = images.PreparedImage(Image.new("RGBA", (1, 1)), None, (1, 1), (0, 0, 1, 1), 1, "PNG")
    checks = iter([False, False, True])
    monkeypatch.setattr(previews, "_decode_response", lambda *args, **kwargs: result)
    with pytest.raises(images.PreviewError, match="cancelled"):
        source.prepare(cancelled=lambda: next(checks))
    with pytest.raises(ValueError, match="closed"):
        result.image.getpixel((0, 0))


def test_connection_error_is_a_preview_fallback():
    connection = Connection()

    def fail(*args, **kwargs):
        raise ValueError("Workstation disconnected")

    connection.request_json = fail
    source = previews.remote_source(connection, "session-a", {"id": 1})
    with pytest.raises(images.PreviewError, match="Workstation disconnected"):
        source.prepare()


def test_remote_widget_never_stats_workstation_paths_preserves_zoom_and_refreshes(monkeypatch):
    pytest.importorskip("textual")
    from textual.app import App
    from textual.widgets import Static

    class FakeNative(Static):
        def __init__(self, **kwargs):
            kwargs.pop("on_error", None)
            super().__init__("", **kwargs)
            self.image = None

    monkeypatch.setattr(images, "_native_widgets", {"sixel": FakeNative})
    monkeypatch.setattr(images, "_signature", lambda *args: pytest.fail("remote widget must not stat locally"))
    connection = Connection()
    file = {"id": 1, "path": "C:\\workstation\\image.png", "preview_revision": "r1"}

    class PreviewApp(App):
        def compose(self):
            yield images.create_image_preview(None, id="preview", capability=images.ImageCapabilities("sixel"))

    async def settle(app, pilot):
        await app.workers.wait_for_complete([worker for worker in app.workers if not worker.is_cancelled])
        await pilot.pause(0.2)

    async def exercise():
        app = PreviewApp()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            widget = app.query_one("#preview")
            widget.set_source(previews.remote_source(connection, "session-a", file))
            await settle(app, pilot)
            widget.set_zoom(2, (0.75, 0.5))
            await settle(app, pilot)
            first = widget._owned_pixels
            calls = len(connection.calls)
            widget.set_source(previews.remote_source(connection, "session-a", dict(file)))
            await settle(app, pilot)
            assert widget._owned_pixels is first
            assert widget.zoom == 2
            assert len(connection.calls) == calls
            connection.response = payload(color="blue")
            file["preview_revision"] = "r2"
            widget.set_source(previews.remote_source(connection, "session-a", file))
            assert widget._image_widget.image is None
            await settle(app, pilot)
            assert widget._owned_pixels.getpixel((0, 0)) == (0, 0, 255, 255)
            with pytest.raises(ValueError, match="closed"):
                first.getpixel((0, 0))
            last = widget._owned_pixels
        with pytest.raises(ValueError, match="closed"):
            last.getpixel((0, 0))

    asyncio.run(exercise())


def test_importing_remote_preview_does_not_load_or_probe_terminal_libraries():
    result = subprocess.run([sys.executable, "-c",
        "import sys; import donedatahoarder.remote.previews; assert 'textual' not in sys.modules; assert 'textual_image' not in sys.modules"],
        check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

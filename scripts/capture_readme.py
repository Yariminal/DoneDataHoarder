"""Export the real Textual workspace using an owned, disposable collection.

Requires the tui extra on Python 3.12+. No model, personal library, fake service,
approvals, or file operations are involved. SVG exports omit terminal graphics.
Run this as a standalone process, not inside an existing DDH session.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_collection(root: Path) -> None:
    from PIL import Image, ImageDraw

    for folder in ("Camera originals", "Shared copies", "Documents"):
        (root / folder).mkdir(parents=True)
    # A drawing with enough structure for real pHash comparisons. The two
    # copies are derived from the same pixels; no duplicate rows are seeded.
    with Image.new("RGB", (1800, 1200), "#1a1b26") as drawing:
        pen = ImageDraw.Draw(drawing)
        pen.rectangle((0, 0, 1800, 700), fill="#7aa2f7")
        pen.ellipse((1200, 100, 1450, 350), fill="#e0af68")
        pen.polygon([(0, 900), (500, 350), (1000, 950)], fill="#414868")
        pen.polygon([(600, 1000), (1350, 450), (1800, 850), (1800, 1200)], fill="#9ece6a")
        pen.rectangle((0, 1050, 1800, 1200), fill="#449dab")
        # The smaller version deliberately retains capture fields absent from
        # the larger export, demonstrating an actual evidence tradeoff.
        drawing.save(root / "Shared copies" / "coast-large-export.jpg", quality=94)
        exif = Image.Exif()
        exif[271] = "Sample camera"
        exif[272] = "Synthetic fixture"
        exif[34665] = {36867: "2024:07:14 18:32:10", 42036: "Sample 35mm", 34855: 100}
        with drawing.resize((1200, 800)) as smaller:
            smaller.save(root / "Camera originals" / "coast-with-capture-data.jpg", quality=94, exif=exif)
    notes = root / "Documents" / "trip-notes.txt"
    notes.write_text("Disposable DDH sample. Coastal walk, July 2024.\n", encoding="utf-8")
    (root / "Documents" / "trip-notes-copy.txt").write_bytes(notes.read_bytes())


async def capture(service, output: Path) -> None:
    from donedatahoarder.tui.app import DDHApp
    from donedatahoarder.tui.images import ImageCapabilities

    def save_svg(name: str, title: str) -> None:
        svg = app.export_screenshot(title=title)
        (output / name).write_text("\n".join(line.rstrip() for line in svg.splitlines()) + "\n", encoding="utf-8")

    app = DDHApp(service, image_capability=ImageCapabilities("off", "Headless documentation capture: terminal images are off."))
    async with app.run_test(size=(140, 46)) as pilot:
        for _ in range(100):
            await pilot.pause(.05)
            if app.snapshot.get("files"):
                break
        if not app.snapshot.get("files"):
            raise RuntimeError("Workspace did not load the generated collection")
        photo = next(file for file in app.snapshot["files"] if file["filename"] == "coast-with-capture-data.jpg")
        app.selected_file_id = photo["id"]
        app.update_inspector()
        await pilot.pause(.2)
        output.mkdir(parents=True, exist_ok=True)
        save_svg("workspace.svg", "DDH / synthetic collection / metadata-only run")

        proposal = next((item for item in app.snapshot["proposals"]
                         if ((item.get("duplicate_evidence") or {}).get("photo_quality") or {}).get("status") == "tradeoff"), None)
        if proposal is None:
            raise RuntimeError("The real pipeline did not produce the expected photo tradeoff")
        app.action_workspace("review")
        app.selected_proposal_id = proposal["id"]
        app.update_review_detail()
        await pilot.pause(.2)
        # Show the evidence section using the same scroll action available to
        # the reader; preserve the full paths and reasoning in the live widget.
        app.query_one("#review-evidence").scroll_end(animate=False)
        await pilot.pause(.1)
        save_svg("photo-review.svg", "DDH / photo keeper evidence / synthetic collection")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "docs" / "assets")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="ddh-readme-") as scratch:
        base = Path(scratch)
        os.environ.pop("NO_COLOR", None)
        os.environ["COLORTERM"] = "truecolor"
        # Keep journals and theme lookup inside the disposable fixture too.
        for variable, folder in (("DDH_DATA_DIR", "journal"), ("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state")):
            os.environ[variable] = str(base / folder)
        from donedatahoarder.tui.qualification import _index_collection
        from donedatahoarder.tui.service import WorkspaceService

        root, database = base / "Sample collection", base / "index.sqlite"
        make_collection(root)
        manifest = _index_collection(root, database)
        service = WorkspaceService(session_id=manifest["session_id"], db_path=database)
        try:
            asyncio.run(capture(service, args.output))
        finally:
            service.engine.dispose()
    print(f"Exported actual headless TUI captures to {args.output}")


if __name__ == "__main__":
    main()

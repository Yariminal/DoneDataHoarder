"""Make an isolated, synthetic UI review session for local visual QA.

Run with the repository venv. Prints DDH_DB and session ID; never touches a
real collection or an existing validation database.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw
from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    DupeType, DuplicateGroup, DuplicateMember, File, FileStatus, Proposal,
    ProposalStatus, ProposalType, SessionStatus, UserSession,
)
from donedatahoarder.db.session import init_db


def main() -> None:
    workspace = Path(tempfile.mkdtemp(prefix="ddh-review-"))
    root = workspace / "Synthetic Collection"
    root.mkdir()
    db_path = workspace / "review.db"
    engine = init_db(db_path)

    def write(rel: str, data: bytes) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    images = []
    for name, color, shape in (
        ("image_001.png", "#e69b4a", "rectangle"),
        ("image_002.png", "#e69b4a", "ellipse"),
    ):
        path = root / name
        im = Image.new("RGB", (520, 320), "#ede7da")
        draw = ImageDraw.Draw(im)
        if shape == "rectangle":
            draw.rounded_rectangle((120, 70, 400, 250), radius=36, fill=color)
        else:
            draw.ellipse((120, 70, 400, 250), fill=color)
        im.save(path)
        images.append(path)
    doc = write("Inbox/scan_001.txt", b"Invoice for September studio supplies. Total EUR 125.00.\n")
    uncertain = write("Inbox/untitled.txt", b"short\n")
    obj = write("Project Alpha/model.obj", b"mtllib model.mtl\no object\n")
    mtl = write("Project Alpha/model.mtl", b"newmtl matte\n")
    page = write("Project Alpha/index.html", b'<img src="assets/texture.png">')
    texture = write("Project Alpha/assets/texture.png", b"synthetic linked texture")

    with Session(engine) as db:
        # This workstation already has gemma4:26b installed. These are
        # session selections for a future run; fixture descriptions below
        # remain explicitly synthetic and do not claim model inference.
        owner = UserSession(
            name="Synthetic review demo", root_path=str(root), status=SessionStatus.ACTIVE,
            backend="ollama", model="gemma4:26b", analyze_model="gemma4:26b",
            propose_model="gemma4:26b", workers=1,
        )
        owner.stats = {"files_count": 8, "proposals_count": 5,
                       "duplicates_count": 1,
                       "completed_steps": ["scan", "enrich", "analyze", "dedup", "relate", "propose", "organize"]}
        db.add(owner)
        db.flush()

        def row(path: Path, description: str | None, *, outcome="content_verified",
                source="vision", status=FileStatus.PROPOSED) -> File:
            content = path.read_bytes()
            f = File(session_id=owner.id, path=str(path), filename=path.name,
                     extension=path.suffix.lower(), size_bytes=len(content),
                     mime_type="image/png" if path.suffix == ".png" else "text/plain",
                     hash_md5=hashlib.md5(content).hexdigest(),
                     hash_sha256=hashlib.sha256(content).hexdigest(),
                     ai_description=description, ai_confidence=0.78 if description else None,
                     analysis_outcome=outcome, analysis_evidence_source=source,
                     analysis_model_tag="synthetic:no-inference", status=status)
            db.add(f)
            db.flush()
            return f

        keeper = row(images[0], "Orange rounded block on a pale ground")
        candidate = row(images[1], "Orange oval object on a pale ground")
        invoice = row(doc, "Invoice for September studio supplies", source="text")
        limited = row(uncertain, None, outcome="context_only", source="filename_only")
        model = row(obj, "3D model with material reference", source="text")
        row(mtl, None, outcome="metadata_only", source="metadata")
        row(page, "Page with a linked texture", source="text")
        row(texture, None, outcome="metadata_only", source="metadata")

        group = DuplicateGroup(session_id=owner.id, dupe_type=DupeType.PERCEPTUAL,
                               group_hash="synthetic-visual-similarity", keep_file_id=keeper.id)
        db.add(group)
        db.flush()
        db.add_all([
            DuplicateMember(group_id=group.id, file_id=keeper.id),
            DuplicateMember(group_id=group.id, file_id=candidate.id,
                            similarity_score=0.91, distance_to_keeper=4),
        ])
        db.add_all([
            Proposal(file_id=invoice.id, proposal_type=ProposalType.RENAME,
                     current_value=invoice.path,
                     proposed_value=str(doc.with_name("September_studio_supplies_invoice.txt")),
                     reasoning="Own verified text identifies the invoice subject; review the title.",
                     confidence=0.78, status=ProposalStatus.PENDING),
            Proposal(file_id=invoice.id, proposal_type=ProposalType.MOVE,
                     current_value=invoice.path,
                     proposed_value=str(root / "Independent_Files" / "Documents" / doc.name),
                     reasoning="Independent document grouped by type.", confidence=0.55,
                     status=ProposalStatus.APPROVED),
            Proposal(file_id=limited.id, proposal_type=ProposalType.ADD_TAGS,
                     current_value=None, proposed_value='["uncertain"]',
                     reasoning="Only filename context was available; verify content manually.",
                     confidence=0.2, status=ProposalStatus.PENDING),
            Proposal(file_id=model.id, proposal_type=ProposalType.RENAME,
                     current_value=model.path,
                     proposed_value=str(obj.with_name("presentation_model.obj")),
                     reasoning="Protected project resource, shown to exercise review guard.",
                     confidence=0.7, status=ProposalStatus.PENDING),
            Proposal(file_id=candidate.id, proposal_type=ProposalType.MARK_DUPLICATE,
                     current_value=candidate.path, proposed_value=keeper.path,
                     reasoning="Visual similarity candidate; distinct shape must be inspected.",
                     confidence=None, status=ProposalStatus.PENDING,
                     duplicate_group_id=group.id),
        ])
        db.commit()
        print(json.dumps({"DDH_DB": str(db_path), "session_id": owner.id,
                          "collection": str(root)}, indent=2))


if __name__ == "__main__":
    main()

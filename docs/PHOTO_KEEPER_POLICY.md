# Photo duplicate keeper policy

Implemented 2026-09-29. Photo keeper recommendations use measured resolution
and structured EXIF evidence, with explicit review for preservation tradeoffs.

## Product purpose

The user's photo collection includes many copies in OneDrive. Their experience
with existing duplicate tools was that the selected keeper could have worse
image quality or less complete EXIF than another copy. DDH must make preserving
the strongest photo and its metadata a primary duplicate-review requirement.

Finding duplicate candidates and choosing the copy worth keeping are separate
decisions. Similarity alone establishes neither interchangeability nor which
file is the best original. The retained photo should have the best available
resolution and the most complete, valid capture metadata. A filename, folder,
cloud/file timestamp, or larger byte count must not override that evidence.

## Implementation

Enrichment stores versioned pixel dimensions, format, and validated EXIF fields
bound to the indexed SHA-256 hash. Review computes oriented display dimensions
from the recorded orientation. Supported fields
include capture time/offset, camera/lens details, exposure, orientation, GPS,
and authorship. Complete, partial, unavailable, and unsupported extraction are
distinct states. Filesystem and AI dates do not contribute to photo ranking.

Both keeper-selection paths use the same deterministic ordering: pixel count,
valid meaningful metadata count, complete evidence, then stable path/id ties.
That ordering chooses a reference photo; the pairwise preservation comparison
checks actual metadata values and distinguishes a recommended keeper from a
tradeoff, variant, equivalent measured evidence, or unknown evidence. Unique
fields and conflicts are shown in terminal and browser review. Non-photo
selection retains its existing date/path/size behavior.

New groups receive this recommendation. Existing keeper choices are preserved
because the database does not distinguish a past automatic choice from an
explicit user choice. Use **Keep A / Keep B** in terminal image comparison or
the browser keeper control to change it. Keeper changes reset affected review
decisions and are available through paired remote sessions as well.

The existing review and execution safeguards remain relevant: visual similarity
requires individual review, keeper changes invalidate prior comparisons, and
filesystem changes require explicit approval. Improving keeper ranking must not
turn a similarity match into automatic permission to discard it.

## Intended decisions

- Extract image dimensions and structured, valid metadata before ranking. Keep
  actual capture metadata distinct from filesystem dates or AI-inferred values.
  Record unsupported/unavailable extraction separately from confirmed absence.
- Prefer a copy that preserves at least as much image detail and meaningful
  metadata as the others. Compare actual metadata fields and values, not just
  a count of arbitrary tags. Relevant evidence includes original capture time,
  camera/lens information, exposure, orientation, and location where present.
- Do not let an older filesystem timestamp or a more descriptive path beat
  a higher-quality, metadata-complete copy. Use stable tie-breaking only after
  preservation-relevant evidence is equivalent.
- When the highest-resolution copy and richest-metadata copy differ, preserve
  both for review. This is the proposed default for the unresolved tradeoff;
  the user's requirement does not establish that either dimension always wins.
  Unique valid metadata and conflicting values must remain visible.
- Do not label greater dimensions as proof of more original detail: upscaled,
  compressed, cropped, edited, or differently rendered versions can have
  distinct value. RAW and developed exports are not automatically disposable
  alternatives. Unknown or conflicting quality evidence calls for review.
- Metadata transfer may be a later, explicit operation after image equivalence
  and field provenance are established. This requirement does not authorize
  silently merging EXIF, changing originals, or dropping conflicting fields.

## Review experience

Keep the existing TUI layout. The comparison pane should explain the proposed
keeper with real evidence, for example: "24 MP vs 6 MP; original capture date,
camera, lens, and exposure retained." Show which metadata only the other copy
contains and distinguish a clear recommendation from a tradeoff needing review.
The user can choose the keeper and inspect the resulting proposed action.

## Existing indexes

The additive database migration leaves old photo evidence unknown. On the
machine that owns the collection, explicitly backfill one saved session:

```sh
ddh refresh-photos --db /path/to/index.db --session SESSION_ID --workers 4
```

This reads local photos, verifies their SHA-256 against the index, and preserves
AI analysis, file status, hashes, and keeper choices. Changed or unavailable
content is marked unknown; re-index changed content before trusting it. Photo
evidence changes invalidate affected approvals, including proposals whose
keeper changed evidence. Applied history and rejections remain intact. An
unchanged repeat refresh leaves review decisions alone. Refreshing performs no
photo writes, metadata merge, or file deletion. Stop active pipeline work first;
the operation uses the same database writer lease as the pipeline.

Force-rescanning a keeper also returns related duplicate approvals to pending
review, including older comparisons linked by keeper path. Re-enrich the files
and review those comparisons again before applying them.

## Limits

This version compares EXIF, not every possible metadata store. RAW and
HEIC/HEIF/AVIF metadata are currently unsupported. Known embedded XMP/IPTC,
multi-frame images, malformed headers, and bounded-read failures cannot
establish complete comparable evidence. Sidecars are not inventoried or compared.
Recognized Windows offline/recall placeholders are not opened for extraction;
the scanner's existing reparse-point exclusions still apply. This is not a new
OneDrive hydration or sync policy. Make the intended files local first.

Resolution is not a sharpness, compression-quality, authenticity, or original
detail score. Crops, different renderings, and differing perceptual fingerprints
remain reviewable variants. No automatic EXIF transfer or consolidation is
implemented. Actual Omarchy terminal graphics and user-labeled OneDrive photos
still need hardware/data qualification; automated tests use synthetic files.

## Acceptance examples for implementation

1. A full-resolution, metadata-complete original beats its resized, stripped
   export even when the export has an earlier file date or a longer path.
2. Equal-resolution copies with identical capture information prefer the one
   retaining additional valid metadata; padding and meaningless tags do not win.
3. A high-resolution file with stripped EXIF and a smaller file with unique
   capture metadata produce a visible tradeoff; neither is silently discarded.
4. Conflicting capture times, crops, edits, upscales, and RAW/export pairs remain
   reviewable variants, without unsupported claims of equivalent image quality.
5. Unreadable or unavailable cloud content is reported as unknown, rather than
   scored as a confirmed metadata-poor copy. No automatic cloud hydration policy
   is introduced by this requirement.
6. The in-memory and chunked database selection paths produce the same decision,
   expose the same reasons, and preserve existing approval/recovery safeguards.

Validate ranking with deliberately constructed pairs plus user-labeled real
examples. Keep candidate-retrieval accuracy separate from keeper-selection
accuracy; a correct ranking does not repair a false duplicate match.

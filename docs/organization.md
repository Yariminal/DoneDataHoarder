# Folder organization and relationship inference

DoneDataHoarder's purpose is to organize mixed collections of files and folders.
Duplicate detection is one part of that workflow. The organization pipeline
draws on what files are called, what analysis can establish about their content,
where they were stored, and their available metadata. It proposes changes for
review instead of treating every inferred relationship as a fact.

## Evidence across the pipeline

| Evidence | Current use |
| --- | --- |
| Filenames and extensions | Naming, semantic relation candidates, versions, source/backup companions, and numbered sequences |
| File content | Supported analyzers produce descriptions, tags, extracted text, and analysis provenance; these inform naming and content-based folder summaries |
| Original folder tree | Directory context scopes relationships; parent/child structure, existing folder themes, and project boundaries inform organization |
| Metadata | File type, size, dates, and extracted format metadata support analysis and naming; photo dimensions and meaningful EXIF additionally support duplicate keeper decisions |
| Existing relationships | Relation groups support companion naming and organization while preserving dependency-bound files and sequences |

These signals are combined across stages, not all fed directly into one model
call. **Relate** currently uses filenames, extensions, sizes, and directory
context, plus deterministic structural rules. It does not read raw content or
all EXIF fields to prove a relationship. **Organize** builds a compact summary
of the actual folder tree, including content-derived tags and description
keywords where analysis supplied them, file categories, sample names, and
outliers. That distinction matters when judging a suggested group.

## What the plan can propose

The naming stage can suggest clearer file names and tag updates. The organizer
can propose moving files, consolidating related folders, assigning loose files
to folders, or renaming a folder in place. Existing organizational intent,
related companions, project boundaries, and dependency checks constrain these
proposals. Recognition has limits; it is still important to inspect projects
whose tools rely on specific paths.

Move and merge suggestions become individual file-move proposals, subject to
the same guards. Current rules limit these to eligible loose files and preserve
named/project folder boundaries; this is not unrestricted whole-folder restructuring.

For example, a CAD source and a same-stem backup are structural companions.
A group of documents with related descriptions and tags can inform a folder's
theme. A file that differs from that theme can be surfaced as a candidate move.
These are examples of evidence the implementation uses, not guarantees that
every project or document will be classified correctly.

## Review the result

Use the full pipeline with an installed Ollama model to include content analysis,
Relate, naming, and Organize. Inspect inferred groups in **Collections**, proposed
renames and moves in **Review**, and the final source/destination plan in
**Preview**. Approval and application are separate actions; recovery uses the
operation journal and requires verifiable file or directory identity.

Metadata-only mode runs Scan → Enrich → Dedup → Preview. It is useful for indexing
and duplicate evidence, but it does **not** demonstrate full semantic folder
organization. Missing extractors, unsupported formats, failed inference, or
bounded candidate coverage reduce the available evidence and must not be read
as successful content understanding.

See [the terminal guide](tui.md), [photo keeper policy](PHOTO_KEEPER_POLICY.md), and
[remote workstation setup](remote-sessions.md) for the other parts of this workflow.

# Sharing the Omarchy preview

## The story

**Organize files by what belongs together.** DoneDataHoarder is a terminal file and folder organizer for mixed collections: documents, projects, media, exports, and backups. Lead with the transformation from the original folder tree to a proposed structure, and show the evidence behind it: filenames, available content analysis, metadata, and relationships. Photo keeper review is a valuable part of that larger story.

The current branch is an alpha preview. Link to the [README](../README.md) for installation and supported behavior. Native terminal graphics, a real Omarchy-to-Windows LAN session, and workstation GPU performance need hardware qualification before making claims about those specific environments.

## A 30-second recording

1. Open a disposable mixed collection. Show the original tree and full pipeline with its actual stage states.
2. Run the full pipeline with a configured model. Show a real inferred collection and explain the available evidence tying its members together.
3. Show proposed folder moves or clearer names, then open the source/destination preview. This is the main payoff: the proposed organization becomes visible before applying it.
4. Briefly show a duplicate-photo tradeoff: the larger copy lacks capture metadata retained by another. Use a qualified terminal for actual image comparison.
5. If the real two-machine setup has been qualified, show the workstation connection indicator. Keep any apply demonstration confined to generated files.

Use actual processing and clearly label any sped-up sections. Keep personal photos, GPS data, private paths, invitation codes, and credentials out of the recording. Do not present generated sample files as a performance or accuracy benchmark.

## Draft posts

**Organization-first:**

> Years of downloads, projects, and exports. DDH uses names, content analysis, metadata, and folder context to propose what belongs together. A full terminal pipeline, reviewable moves, and photo keeper evidence. Built for the Omarchy workflow. Alpha: [repo link]

**Photo feature follow-up:**

> One photo has more pixels. Another kept the capture date and lens metadata. Which survives cleanup? DDH shows the tradeoff and lets you choose, as part of its wider file and folder organization workflow. Alpha preview: [repo link]

**Workstation follow-up, after hardware qualification:**

> My Omarchy laptop gets the TUI. My Windows workstation does the heavy lifting on the collection attached to it. DDH adds nearby pairing while keeping the same organization and review workflow. Alpha preview: [repo link]

These are drafts for you to edit and post. A clear demonstration and a reproducible install are more useful than promising perfect duplicate detection or a viral launch.

## Assets and evidence

The README's SVGs are exported from the actual Textual app using synthetic files and a real metadata-only pipeline. They illustrate the workspace and photo evidence; they do not demonstrate semantic grouping or folder organization. Record a separate full-pipeline run with a configured model for the organization demo above. Regenerate the SVGs with:

```bash
python scripts/capture_readme.py
```

The script owns a temporary collection and index; it does not approve or apply proposals. Native Sixel/Kitty pixels are absent from headless SVG exports. See [terminal qualification](tui-qualification.md) to record actual photo rendering.

Before sharing, check that the linked branch and installation instructions match, every required GitHub check on that commit is green, the screenshots match current behavior, and any hardware claims have corresponding recorded checks. The preview does not enforce external-SSD-only access, hydrate OneDrive placeholders, merge EXIF into files, or rank optical sharpness.

# Native terminal image qualification

This walkthrough qualifies the real Python/Textual application. Run it inside
the terminal being tested on Omarchy. A headless test, a positive graphics
probe, or an exported SVG does not establish that native photos render cleanly.

The current native status is **pending**. Windows headless tests cover image
lifecycle, but this workspace has not run a native Foot or Kitty walkthrough.

**Prepare a disposable collection.** Install this candidate with the TUI extra
using the [installation guide](tui-installation.md), then run:

```bash
kit="$HOME/ddh-native-$(date +%Y%m%d-%H%M%S)"
ddh tui-fixture "$kit"
cat "$kit/START.txt"
```

The target directory must not exist. The command creates 13 synthetic diagnostic
drawings, a manifest, and an isolated `index.sqlite`. It runs the real
Scan → Enrich → Dedup → Preview metadata pipeline. It makes no AI request and
approves/applies no proposals. After indexing, it deliberately removes one
generated file and replaces another to test stale data. Other folders and
existing databases are outside this operation.

The samples include an exact PNG pair, edited/cropped variants, EXIF rotation,
a portrait, alpha transparency, an animated GIF, a 12 MP JPEG, a Unicode path,
and malformed/missing/changed files. They are diagnostic drawings, not a
photographic benchmark or evidence of useful AI proposals. Some metadata-reader
warnings are expected for the deliberate malformed file and formats without
EXIF. `manifest.json` records expected appearances and generated/current hashes.

`--no-index` generates only the corpus; it leaves the missing/changed cases
unmodified and does not create a ready session. Use the default command for this
walkthrough. A failed preparation stays recorded in the manifest; use a new
directory for another attempt rather than overwriting the failed evidence.

**Capture terminal diagnostics before opening the app.**

```bash
ddh tui-diagnostics --images auto --terminal-name foot \
  --terminal-version YOUR_FOOT_VERSION --omarchy-version YOUR_OMARCHY_VERSION \
  --output "$kit/foot-report.json"
```

Use the actual installed name/version; `TERM` is only a hint. The terminal can
be Foot, Kitty, or the installed default terminal. Record each separately.
Write to `--output` while stdout remains attached to the terminal: redirecting
stdout to a file disables probing because it is no longer a graphics terminal.
The command never overwrites an existing report and makes no database or Ollama
request. It collects an allowlist of terminal hints, runtime/package versions,
graphics capability, cell size, and a checklist initially marked `not_run`.

For reproducibility, fill in the report's font/size, display scale, reference
machine, tmux version if applicable, and fixture-manifest path. Record the
source revision or candidate artifact hash in `notes`. Cell dimensions when the
renderer is off are fallback values, not measurements. Probe duration is not
startup or visible-image latency.

**Open the session using the exact command in `START.txt`.**

It has this form:

```bash
ddh tui --db "$kit/index.sqlite" --session SESSION_ID
```

Do not rerun metadata before exercising the missing/changed-file cases; that
would refresh the indexed baseline. Start at 120×40 or wider. Use `?` or F1 for
the keyboard reference, arrows/Tab to navigate, `i` to enlarge an image, `c` to
compare, and Esc to close a dialog. Opening the fixture does not execute files
or apply any proposed changes.

| Check | Action and expected result |
| --- | --- |
| Selection | Alternate quickly between files. Image, path, dimensions, and evidence always refer to the same selection. |
| Comparison | Select `00-exact/original.png`, press `c`, and inspect both original/copy panes. Each native image renders completely. |
| Zoom/pan | Use the image dialog's linked controls, then fit again. Aspect ratio stays correct and original detail appears as you zoom. |
| Orientation | EXIF sample displays 800×1200 after clockwise rotation; plain portrait stays tall. |
| Formats | Transparent sample retains alpha; GIF shows the first frame and explicitly says it is a static first-frame preview. |
| Resize | Exercise 80×24, 120×40, and wide sizes while comparison is open, then close it. Controls remain reachable and no image crosses a border. |
| Scroll | Scroll/select through the tree and comparison candidates. Check repeated redraws for ghost images and distracting flicker. |
| Theme | Change between a light and dark Omarchy theme. UI colors reload; photo pixels retain their colors. |
| Modals | Open and close help, edit, and confirmation dialogs where available. Images do not appear over a covering dialog or remain after it closes. Cancel review actions. |
| Fallbacks | Missing/corrupt files show readable errors. The changed sample shows purple NEW CONTENT; stale indexed hashes cannot establish an exact copy. |
| External viewer | Open an existing image externally and verify its path/content. A missing file reports an error without opening a different file. |
| Exit | Close comparison, reopen it repeatedly, then quit. Images clear and ordinary terminal input works afterward. |

Repeat candidate navigation in a real photo collection and a large duplicate
group before claiming broad support; this small fixture does not cover those
workloads. Test tmux separately from direct terminal execution. Test explicit
`--images sixel`, `--images kitty`, and `--images off` as applicable, including
their unsupported-path explanations.

**Record observed outcomes.** Edit each JSON check to `pass`, `fail`, or
`not_run`, add concise notes, and attach local recording/screenshot paths to its
`evidence` list. Set overall `qualification` to `pass` only after every required
check passes on that exact environment. Preserve failures and untested cases;
capability detection never changes their result automatically.

For performance, use at least 100 interactions on a named machine. Record
visible selection-to-frame/input timing, median/p95, and peak Python RSS. Keep
Ollama/GPU memory separate. Image decoding time alone does not measure visible
rendering. The 12 MP drawing is useful for repeatability but must be supplemented
with real photographs for cold-preview claims.

| Environment | Qualification |
| --- | --- |
| Foot, direct, Sixel | Pending native run |
| Kitty, direct, Kitty graphics | Pending native run |
| Installed Omarchy default terminal | Record actual terminal; pending native run |
| Ghostty | Pending native run |
| tmux plus outer terminal | Separate pending configuration |
| Windows headless Textual | Automated layout/lifecycle evidence only |

The next milestone uses these findings to fix redraw/cleanup behavior before
adding image effects. See the [delivery plan](PYTHON_TEXTUAL_PLAN.md) for the
performance budgets and complete release gates.

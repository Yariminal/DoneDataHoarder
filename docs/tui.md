# Terminal workspace for Omarchy

`ddh tui` opens the file pipeline and review interface in a terminal. It uses the
same SQLite sessions, durable jobs, proposal engine, and execution journal as the
CLI and web interface. Opening a folder does not start processing or move files.

## Install and open

The terminal extra requires Python 3.12 or newer. The core CLI remains compatible
with Python 3.10+. From a checkout containing the TUI:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[tui,docs]'
ddh tui
# Or open a folder directly:
ddh tui ~/Downloads
```

`docs` adds PDF and Office text extraction. It is optional; `tui` alone includes
the terminal and image dependencies. Do not install packages into Arch's system
Python environment.

```bash
# Use your installed local model and a specific index.
ddh tui ~/Downloads --model gemma3:12b --db ~/.local/share/donedatahoarder/index.db

# Return to the session ID displayed by the workspace.
ddh tui --session SESSION_ID

# Open a database created by existing CLI or web commands.
ddh tui --db /path/to/donedatahoarder.db --session SESSION_ID

# An explicit renderer override, or metadata plus the desktop image viewer.
ddh tui ~/Pictures --images sixel
ddh tui ~/Pictures --images off
```

The default index is `$XDG_DATA_HOME/donedatahoarder/index.db`, falling back to
`~/.local/share/donedatahoarder/index.db`. `DDH_DB` and `--db` override it. The folder
argument is optional: without a folder or `--session`, a picker offers a new
folder or recent session without creating a session prematurely. A folder
cannot accompany `--session`. `--model` and `--workers` configure a new session; reopening a
session preserves its saved model and worker settings. New run plans record
`--ollama-host`, and resuming a plan preserves that plan's provider settings.
The TUI explicitly selects Ollama and never silently switches to Gemini.
The picker can check installed Ollama models without downloading or running one.
Metadata-only processing remains available while Ollama is offline.
See the [installation guide](tui-installation.md) for isolated tool installation.

## Pipeline and review

The full run is Scan → Enrich → Analyze → Dedup → Relate → Propose → Organize →
Preview. Progress comes from actual jobs and checkpoints. Missing measurements
are not replaced with simulated rates or percentages.

The metadata-only action runs Scan → Enrich → Dedup → Preview. It works without
an AI server and marks AI-dependent phases as skipped. It still indexes files,
extracts metadata, and produces duplicate review proposals.

Review proposals individually, inspect the evidence, and approve, reject, or edit
them. Exact duplicates and similar-image candidates remain distinct; a visual
match alone does not establish that a file can be deleted. Execution needs a
fresh preview of the approved actions and a separate confirmation. Changing a
review decision or destination invalidates an earlier preview.

The History workspace exposes journaled operations and a recovery preview.
Recovery is scoped to the session and checks for conflicts; it is not a global
filesystem rollback. Keep the workspace open while a commit or recovery is
running. Interrupted processing resumes only through an explicit user action.

## Keyboard navigation

Keyboard shortcuts:

| Key | Action |
| --- | --- |
| `1` / `2` / `3` / `4` | Pipeline / Review / Collections / History |
| `Tab`, arrows, `j` / `k` | Move focus and navigate files |
| `Space` | Run, pause, or explicitly resume the pipeline |
| `m` | Start a metadata-only run |
| `a` / `r` / `e` | Approve / reject / edit the selected item in Review |
| `p` | Preview approved changes before a separate confirmation |
| `i` / `c` | Enlarge an image / compare duplicate candidates |
| `+` / `-` / `0` | Zoom in / zoom out / fit in the image view |
| `Esc` | Close a dialog |
| `o` | Choose a folder or recent session while idle |
| `?` / `F1` | Open the keyboard reference |
| `q`, `Ctrl+C`, `Ctrl+Q` | Quit; active work must stop safely first |

## Photos in the terminal

The selected-file inspector shows a local thumbnail. The image view enlarges a
photo, and duplicate comparison presents the candidate and keeper together with
their paths and evidence. Original colors and aspect ratio are preserved; the
Omarchy palette affects the interface chrome only. Image preparation happens
away from the UI thread, with bounded decoding and thumbnail caching.

Image capability is checked **before** Textual takes ownership of terminal input.
Choose `--images auto`, `sixel`, `kitty`, or `off`. An unsupported renderer or image
gets an explanatory fallback and **Open original**, which invokes the desktop
viewer without a shell command. Production previews read local files; they do not
upload images or load the browser demo's sample photograph.
Repeated unchanged selections retain their pixels and zoom. Animated formats
display a labeled first-frame preview rather than playing the animation.

Use `ddh tui-fixture NEW_DIRECTORY` to create a disposable indexed image kit,
and `ddh tui-diagnostics --output NEW_REPORT.json` to capture terminal capability
and an uncompleted native checklist. Follow the [native qualification guide](tui-qualification.md).

| Terminal | Rendering path | Validation status |
| --- | --- | --- |
| Foot | Sixel | Primary Omarchy target; upstream library reports support; native DDH smoke test still required |
| Kitty | Kitty graphics | Upstream library reports support; native DDH smoke test still required |
| Ghostty | Kitty graphics | Terminal supports the protocol; DDH/widget compatibility needs native testing |
| Stock Alacritty / unsupported terminals | External viewer | Native raster rendering is not assumed |
| tmux | Depends on server build, passthrough, and outer terminal | Separate configuration requiring native testing |

Terminal rendering support does not automatically guarantee that images clear
correctly when switching panes, opening a modal, or resizing. Before calling a
terminal configuration supported, check those transitions with a real image,
check both sides of a comparison, change theme, and exercise the error fallback.
No tmux configuration is rewritten automatically.

References: [Foot's Sixel setting](https://man.archlinux.org/man/foot.ini.5.en#sixel),
[textual-image terminal matrix](https://github.com/lnqs/textual-image#supported-terminals),
[Ghostty graphics features](https://ghostty.org/docs/features).

## Omarchy themes

The workspace reads the active `colors.toml` at
`$XDG_STATE_HOME/omarchy/current/theme/colors.toml` (normally
`~/.local/state/omarchy/current/theme/colors.toml`). It also supports the older
`$XDG_CONFIG_HOME/omarchy/current/theme/colors.toml` location. Missing or invalid
palettes fall back to Tokyo Night. A theme change reloads interface colors while
preserving the images' colors.

## Development verification

```bash
python -m pip install -e '.[tui,dev,web]'
python -m pytest tests/test_tui_cli.py tests/test_tui_service.py tests/test_tui_app.py tests/test_tui_images.py tests/test_tui_theme.py
```

Headless Textual tests exercise layout, navigation, and actions. Service tests use
temporary collections and SQLite databases to verify session ownership,
duplicate review, stale previews, and execution/recovery. They do not substitute
for native Foot/Sixel or Kitty graphics testing. CI installs the optional TUI on
Python 3.12; Python 3.10 and 3.11 continue to exercise the core application without
requiring the terminal dependencies.

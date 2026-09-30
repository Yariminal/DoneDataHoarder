# Contributing

DoneDataHoarder is an alpha file organizer. Changes must preserve the review boundary: opening a workspace, scanning, and analyzing do not approve filesystem changes. Use disposable collections for development, never your only copy of a photo library.

## Development setup

Use Python 3.12+ to include the Textual interface:

```bash
git clone --branch codex/omarchy-tui https://github.com/Yariminal/DoneDataHoarder.git
cd DoneDataHoarder
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,tui,web,remote,nearby,docs]'
python -m pytest tests/ -q
```

On Windows activate `.venv\Scripts\Activate.ps1` and use its Python executable. The core CI matrix also covers Python 3.10 and 3.11, where TUI tests are skipped. No Ollama server or real collection is required by the automated suite. `tests/test_pipeline.py` is a manual live-server script and is excluded from pytest.

Run the browser state checks with Node.js:

```bash
node --check donedatahoarder/web/static/app.js
for check in tests/ui_*.cjs; do node "$check" || exit; done
```

PowerShell equivalent:

```powershell
Get-ChildItem tests/ui_*.cjs | ForEach-Object {
    node $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "UI check failed" }
}
```

For packaging changes, build and run the isolated artifact checker described in [distribution qualification](docs/tui-installation.md). For UI changes, run relevant headless tests and use the [native terminal checklist](docs/tui-qualification.md) when claiming image-rendering support on a specific terminal.

## Where changes belong

- `core/`, `analyzers/`, `proposals/`, and `executor.py` hold shared processing and file-operation rules.
- `tui/service.py` exposes session-scoped operations. `tui/app.py` renders the terminal workspace.
- `remote/` owns authentication, discovery, command receipts, previews, and the workstation transport.
- `web/` is the browser interface; keep its review rules aligned with the shared engine.
- `tests/` contains synthetic fixtures and regression checks. Every data-integrity fix should include a test that fails before the fix.

Keep slow work off the UI thread. Bind decisions to the displayed session and evidence. Recheck file bytes before mutation. Preserve operation journals and uncertain remote outcomes; never retry a mutation merely because its reply was lost. Do not silently choose a photo copy when resolution and capture metadata conflict.

## Reporting bugs and submitting changes

Include the commit, Python/OS/terminal versions, a small synthetic reproduction, expected behavior, and actual behavior. Remove personal paths, photo metadata, connection tokens, pairing invitations, and private keys from shared logs. Do not attach your real photo library.

Keep pull requests focused and describe the changed behavior, validation, and any untested hardware assumptions. New dependencies should have a clear purpose and belong in the appropriate extra when optional. Contributions use the repository's [MIT license](LICENSE).

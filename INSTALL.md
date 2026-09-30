# Install the Omarchy preview

The Python + Textual interface and nearby workstation sessions are an **alpha preview** on `codex/omarchy-tui`. Use this checkout for the features described here; the published PyPI package may differ. The TUI needs Python 3.12 or newer. The CLI supports Python 3.10 or newer.

## Linux / Omarchy

With Git and [uv](https://docs.astral.sh/uv/getting-started/installation/) available:

```bash
git clone --branch codex/omarchy-tui https://github.com/Yariminal/DoneDataHoarder.git
cd DoneDataHoarder
uv tool install --python 3.12 '.[tui,docs,nearby]'
ddh tui
```

If `ddh` is not found, run `uv tool update-shell` and open a new shell. To install without uv, use Python 3.12+:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install '.[tui,docs,nearby]'
ddh tui
```

Start with disposable sample files. Choose a new, empty location for the fixture, then select its saved session in the TUI:

```bash
ddh tui-fixture /tmp/ddh-preview
ddh tui --db /tmp/ddh-preview/index.sqlite
```

The fixture intentionally includes review and error states. It does not approve or apply changes. For your own folder:

```bash
ddh tui ~/Downloads
```

Press `m` for a metadata-only run without Ollama. See [TUI controls](docs/tui.md) for the complete workflow. Review the proposed changes before applying them; keep a separate backup of valuable files.

## Windows workstation

Install Git and uv, clone the same branch, and run this from the checkout in PowerShell:

```powershell
uv tool install --python 3.12 '.[remote,nearby,docs]'
ddh remote-serve --help
```

Follow [remote sessions](docs/remote-sessions.md) to allow a folder, start the service, and pair the laptop. The workstation owns the files and performs processing. An external SSD is a supported intended workflow, but the current policy is a folder allowlist; automatic SSD-only enforcement is not implemented. Pair only on a trusted network and keep the service credentials private.

## Optional capabilities

Install the extras you need together, for example `uv tool install --force --python 3.12 '.[tui,docs,nearby,video]'` from the checkout.

| Extra | Adds |
| --- | --- |
| `tui` | Textual interface and terminal image rendering; Python 3.12+ |
| `docs` | PDF, Word, and spreadsheet text extraction |
| `nearby` | Local network discovery and pairing support |
| `remote` | Workstation HTTP service |
| `web` | Browser interface |
| `video` | Video/audio processing dependencies; also requires FFmpeg on `PATH` |
| `cloud` | Legacy Gemini integration, separate from the local Ollama TUI workflow |
| `dev` | Development and test dependencies |

FFmpeg is an external executable. Install it with your operating system's package manager and verify `ffmpeg -version`. Optional extraction tools and file formats have limits; a missing extractor does not mean that file content was successfully analyzed. The legacy cloud extra uses a deprecated Google SDK and is not the recommended preview path. No Docker image or Dockerfile is supplied by this repository.

## Ollama

Install and start [Ollama](https://ollama.com/), then install a model appropriate for your machine and content. DoneDataHoarder does not automatically download a model. Check the models you actually have:

```bash
ollama list
ddh tui ~/Downloads --model YOUR_INSTALLED_VISION_MODEL
```

Replace the placeholder with an installed model name. Image analysis needs a vision-capable model. The default Ollama endpoint is `http://localhost:11434`; for remote sessions it is resolved on the workstation. A metadata-only run does not require Ollama.

## Indexes and upgrades

The TUI uses an XDG application data location by default; older CLI commands default to `donedatahoarder.db` in the current directory. Pass the same `--db` path to commands that should share an index. The index contains session state and the recovery journal, so preserve it along with your files.

To add photo evidence to an existing index without repeating AI analysis:

```bash
ddh refresh-photos --db /path/to/index.sqlite --session SESSION_ID
```

See the [photo keeper policy](docs/PHOTO_KEEPER_POLICY.md) for formats, ranking, tradeoffs, and stale-evidence handling. This command does not merge or write EXIF metadata into photos.

Finish active work before upgrading. Save the index and journal, update the intended branch, then reinstall your chosen extras with `uv tool install --force`. `uv tool uninstall donedatahoarder` removes the installed application, not your collection or saved index.

## Troubleshooting

- **No image preview:** use the Images control to try Sixel or Kitty in a compatible terminal, or turn images off. Headless tests do not qualify native rendering on your terminal.
- **No workstation discovered:** discovery needs multicast on the same network. Check the firewall and use the manual connection path described in the remote guide.
- **Database locked:** close other processes using that index and retry after their work finishes. Do not terminate unrelated Python processes.
- **Need a diagnostic report:** run `ddh tui-diagnostics --help`; redact private paths and connection details before sharing output.

For clean package verification and platform limits, see [distribution qualification](docs/tui-installation.md). For development setup and tests, see [CONTRIBUTING.md](CONTRIBUTING.md).

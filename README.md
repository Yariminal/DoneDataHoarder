# DataHoarder

AI-powered file organization for data hoarders.

Transform chaotic folders of photos, documents, and media into a neatly organized, deduplicated, and AI-tagged archive.

## Features

- **Scan & Index** — Walk any directory tree and build a searchable SQLite index.
- **AI Analysis** — Local (Ollama) or cloud (Gemini) vision + text analysis describes your files.
- **Smart Renaming** — Produces meaningful filenames like `2023-07-04_family_bbq.jpg` instead of `IMG_1234.jpg`.
- **Deduplication** — Find exact and near-duplicate images (perceptual hashing).
- **Circuit Breaker** — Automatically fails over to Gemini if Ollama becomes unstable.
- **Web Review UI** — Browser-based gallery to inspect and approve every proposal before it touches disk.
- **Cross-Platform** — Windows, macOS, Linux with proper birthtime detection on each.
- **Hebrew Support** — Safely handles Hebrew and other non-Latin filenames.

## Quick Start

```bash
# Install
pip install datahoarder

# Check your setup
datahoarder doctor

# Scan a folder
datahoarder scan /path/to/messy-folder

# Run the full pipeline (dry-run by default)
datahoarder pipeline /path/to/messy-folder

# Review proposals in the browser
datahoarder serve
# open http://localhost:8080

# Apply approved changes for real
datahoarder execute --commit
```

## CLI Commands

| Command | Purpose |
|---------|---------|
| `doctor` | Diagnose Ollama, disk space, and DB integrity |
| `scan` | Index a directory into the database |
| `enrich` | Extract metadata, hashes, and dates |
| `dedup` | Find exact and near-duplicate files |
| `analyze` | AI vision + text analysis |
| `relate` | Group conceptually related files (CAD + exports + backups) |
| `propose` | Generate rename / tag proposals |
| `review` | Inspect proposals before applying |
| `execute` | Apply proposals (dry-run unless `--commit`) |
| `undo` | Reverse recent operations |
| `stats` | Database statistics |
| `pipeline` | Run scan → enrich → dedup → analyze → propose in one go |
| `serve` | Launch the web review UI |
| `config` | Manage naming rules and preferences |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DATAHOARDER_DB` | SQLite database path |
| `DATAHOARDER_BACKEND` | `ollama`, `gemini`, or `auto` |
| `DATAHOARDER_MODEL` | Ollama model name (default `gemma3:12b`) |
| `OLLAMA_HOST` | Ollama server URL |
| `GEMINI_API_KEY` | Gemini API key (for cloud fallback) |
| `DATAHOARDER_LOG` | `DEBUG`, `INFO`, `WARNING` |

## Test Automation

Run automated end-to-end tests on zipped fixtures and get AI-powered critique of the organization results.

```bash
# 1. Create test_fixtures/ and add .zip files of messy folders
mkdir test_fixtures
cp ~/my_test_data.zip test_fixtures/

# 2. Run the automated test harness
python scripts/test_runner.py test_fixtures/

# 3. Review generated reports
ls test_outputs/test_reports/
```

What it does for each `.zip` fixture:
1. Unzips into a temp directory
2. Runs the full `datahoarder pipeline`
3. Applies all proposals (`execute --commit`)
4. Captures the before/after directory trees
5. Queries the test DB for stats and proposals
6. Asks an LLM to critique the results (scores, strengths, missed opportunities)
7. Saves the DB to `test_outputs/test_dbs/` and a Markdown report to `test_outputs/test_reports/`

Use `--skip-execute` to run the pipeline without touching files (dry-run critique only).
Use `--backend gemini` if Ollama is unavailable for the critique step.

## Documentation

- [Installation Guide](INSTALL.md)
- [CLI Help](https://github.com/Yariminal/DoneDataHoarder/wiki)

## License

MIT

# DoneDataHoarder

**AI-powered file organization for data hoarders** — transform chaotic folders of photos, documents, and media into a neatly organized, deduplicated, and AI-tagged archive.

---

## 🎯 What It Does

DoneDataHoarder is a **local-first, AI-powered file organizer** that:

- **Scans & Indexes** — Walks any directory tree and builds a searchable SQLite database
- **AI Analysis** — Uses local LLM (Ollama) or cloud AI (Gemini) for vision + text analysis
- **Smart Renaming** — Generates meaningful filenames (e.g., `2023-07-04_family_bbq.jpg` instead of `IMG_1234.jpg`)
- **Deduplication** — Finds exact and near-duplicate files using perceptual hashing
- **Automatic Organization** — Groups related files (CAD models + exports, photos + backups, etc.)
- **Cross-Script Support** — Safely handles Hebrew, Arabic, Chinese, and other non-Latin scripts
- **Web Review UI** — Browser-based gallery to inspect and approve changes before they touch disk
- **Safe by Default** — All operations are dry-run by default; you explicitly approve and commit changes

---

## ✨ Key Features

### Smart File Discovery
- Detects vision-capable models (LLaVA, BakLLaVA, Gemma3-vision, etc.)
- Automatic circuit-breaker fallback: Ollama → Gemini if local inference fails
- Graceful degradation for optional dependencies (ffmpeg, faster-whisper, pdfplumber)

### Intelligent Deduplication
- **Exact deduplication** — Remove byte-for-byte identical files
- **Near-duplicate detection** — Perceptual hashing identifies visually/structurally similar files
- **Configurable similarity threshold** — Fine-tune what counts as a duplicate

### Relationship Grouping
- Groups related files by folder structure and LLM reasoning
- Recognizes project patterns: CAD files + exports, documents + versions, photos + edits
- Cross-script linking: Hebrew files linked to English equivalents

### Robust Organization
- **Deterministic folder naming** — Consistent naming across runs
- **Collision handling** — Automatic `_1`, `_2` suffixes for conflicts
- **Prefix preservation** — Maintains project identifiers in filenames
- **Generic stem detection** — Prevents uninformative names like "drawing.pdf" when duplicates exist

### Production-Ready Stability
- **SQLite WAL mode** — Prevents "database is locked" errors during long runs
- **Configurable timeouts** — Handle slow hardware or reasoning models gracefully
- **Clean shutdown** — Proper resource cleanup and session management

---

## 🚀 Quick Start

### Install
```bash
pip install donedatahoarder
```

### Check Your Setup
```bash
ddh doctor
```

### Scan a Folder (Dry-Run)
```bash
ddh scan /path/to/messy-folder
```

### Run Full Pipeline
```bash
# Everything from scan through propose (dry-run)
ddh pipeline /path/to/messy-folder
```

### Review Proposals in Browser
```bash
ddh serve
# Open http://localhost:8080 in your browser
```

### Apply Approved Changes
```bash
ddh execute --commit
```

---

## 📋 CLI Commands

| Command | Purpose |
|---------|---------|
| `doctor` | Diagnose Ollama availability, disk space, and database integrity |
| `scan` | Index a directory tree into the database |
| `enrich` | Extract metadata, file hashes, and accurate modification dates |
| `dedup` | Find exact and near-duplicate files |
| `analyze` | AI vision + text analysis for every file |
| `relate` | Group conceptually related files (CAD + exports, docs + versions, etc.) |
| `propose` | Generate rename / tag / move proposals |
| `review` | Interactive inspection of proposals before applying |
| `execute` | Apply proposals (dry-run by default, use `--commit` to persist) |
| `undo` | Reverse recent operations (restores from trash) |
| `stats` | View database statistics and session history |
| `pipeline` | Run scan → enrich → dedup → analyze → relate → propose in one go |
| `serve` | Launch web UI for reviewing and approving proposals |
| `models` | List available Ollama models |
| `bench` | Benchmark AI performance on sample files |
| `config` | Manage naming rules and preferences |

---

## 🔧 Environment Variables

### Core Configuration
| Variable | Default | Description |
|----------|---------|-------------|
| `DDH_DB` | `./ddh.db` | SQLite database path |
| `DDH_BACKEND` | `auto` | LLM backend: `ollama`, `gemini`, or `auto` |
| `DDH_MODEL` | `gemma3:12b` | Default text model (Ollama) |
| `DDH_VISION_MODEL` | `gemma3:12b` | Vision-capable model (Ollama) |

### Backend-Specific
| Variable | Description |
|----------|-------------|
| `OLLAMA_HOST` | Ollama server URL (default: `http://localhost:11434`) |
| `DDH_OLLAMA_TIMEOUT` | Per-request timeout in seconds (default: `300` = 5 min) |
| `GEMINI_API_KEY` | Gemini API key for cloud fallback |

### Logging & Debugging
| Variable | Default | Values |
|----------|---------|--------|
| `DDH_LOG` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## 📦 Installation by Feature

### Minimum (Images & Documents)
```bash
pip install donedatahoarder
# Works: JPG, PNG, GIF, WEBP, PDF, DOCX, XLSX, TXT, etc.
```

### + Video Support
```bash
pip install donedatahoarder[video]
# Also requires ffmpeg binary:
# Windows: choco install ffmpeg
# macOS: brew install ffmpeg
# Linux: apt-get install ffmpeg
```

### + Audio Transcription
```bash
pip install donedatahoarder[video]  # Includes faster-whisper
```

### + PDF / Office Document Support
```bash
pip install donedatahoarder[docs]
# Includes: pdfplumber, python-docx, openpyxl
```

### + Cloud Fallback (Gemini)
```bash
pip install donedatahoarder[cloud]
export GEMINI_API_KEY=your-api-key
```

### Everything
```bash
pip install donedatahoarder[all]
```

For detailed installation instructions, see [INSTALL.md](INSTALL.md).

---

## 💡 How It Works

### 1. **Scan**
Walks your directory tree and indexes metadata (size, dates, extension) into SQLite. Skips system files, caches, and junk.

### 2. **Enrich**
Extracts:
- File hashes (MD5, SHA256)
- Perceptual hashes (for image deduplication)
- EXIF data (photos: camera, ISO, GPS, etc.)
- Audio metadata (ID3 tags, duration)
- Document metadata (creator, modified date)

### 3. **Dedup**
Groups files by hash:
- **Exact duplicates** → Mark for deletion
- **Near-duplicates** (perceptual hash similarity ≥92%) → Flag for review

### 4. **Analyze**
For each file, run AI vision + text analysis:
- **Images**: Describe content, objects, OCR text
- **PDFs**: Extract text, OCR scanned pages
- **Videos**: Extract keyframes, optionally transcribe audio
- **Documents**: Extract full text
- **Audio**: Transcribe speech-to-text

### 5. **Relate**
Group files by semantic relationship:
- Same project (CAD + exports + backups)
- Same event (photos + videos + edited copies)
- Same publication (doc versions, translations)
- Numeric prefix matching (project IDs)
- Cross-script matching (Hebrew ↔ English equivalents)

### 6. **Propose**
Generate proposals for each file:
- **RENAME**: Meaningful filename based on AI analysis + relationships
- **MOVE**: Reorganize into project folders
- **TAG**: Add semantic tags
- **MARK_DUPLICATE**: Flag as duplicate

### 7. **Review**
Browse proposals in the web UI, adjust confidence thresholds, and approve before applying.

### 8. **Execute**
Apply approved proposals:
- Renames (with collision detection and backoff)
- Moves (creating folders as needed)
- Trash/delete duplicates
- Updates database metadata

---

## 🛡️ Safety & Reliability

- **Dry-run by default** — Nothing changes on disk until you explicitly use `--commit`
- **SQLite WAL mode** — Prevents database-locked errors on long operations
- **Configurable confidence thresholds** — Only apply changes you trust
- **Reversible operations** — `undo` command restores from `.ddh_trash`
- **Session tracking** — Every operation logged with timestamps and diffs
- **No external scanning** — All analysis happens locally or with your Gemini API key

---

## 🌍 Language Support

DoneDataHoarder handles **any Unicode filename**:
- ✅ Hebrew (`מידול.dwg` → `midul_3d_modeling.dwg`)
- ✅ Arabic, Chinese, Japanese, Korean, Russian, etc.
- ✅ Mixed scripts in a single directory
- ✅ Accent characters and diacritics

---

## 🔄 Workflow Example

**Before:**
```
Downloads/
  IMG_1234.jpg
  photo (1).jpg
  myfile.pdf
  scan_001.pdf
  scan_002.pdf
  בדיקה.docx
  בדיקה (1).docx
```

**After running `ddh pipeline Downloads/ && ddh execute --commit`:**
```
Downloads/
  2024-01-15_family_bbq.jpg           (was IMG_1234.jpg)
  2024-01-15_family_bbq_alt.jpg       (was photo (1).jpg)
  inspection_report_2024.pdf          (was myfile.pdf)
  parking_survey_page_1.pdf           (was scan_001.pdf)
  parking_survey_page_2.pdf           (was scan_002.pdf)
  .ddh_trash/
    inspection_report_2024_dup.docx   (was בדיקה (1).docx → duplicate)
```

---

## 📊 Database Schema

DoneDataHoarder uses SQLite with the following main tables:

- **File** — Indexed files with paths, sizes, hashes, metadata
- **Proposal** — Rename/move/tag proposals with confidence scores
- **DuplicateGroup** — Grouped near-duplicates for review
- **RelationGroup** — Semantically related files (projects, events, etc.)
- **UserSession** — Tracks scanning/analysis sessions with progress

No external dependencies needed; everything is self-contained.

---

## ⚙️ Advanced Configuration

### Custom Naming Rules
```bash
ddh config set naming.date_format "%Y-%m-%d"
ddh config set naming.prefer_original_stem true
```

### Adjust Confidence Thresholds
```bash
ddh propose --min-confidence 0.7  # Only high-confidence proposals
ddh execute --confidence 0.6      # Apply medium-confidence changes
```

### Selective Processing
```bash
# Only analyze images, skip videos
ddh analyze --include "*.jpg" "*.png" --exclude "*.mp4"

# Only dedup PDFs
ddh dedup --include "*.pdf"
```

---

## 🐛 Troubleshooting

### "Ollama model not found"
```bash
ollama pull gemma3:12b
# Or use Gemini backend:
export GEMINI_API_KEY=your-key
export DDH_BACKEND=gemini
```

### "Database is locked"
Another DoneDataHoarder process is running. Close other terminals/web UI:
```bash
# macOS/Linux:
pkill -f ddh

# Windows:
taskkill /F /IM python.exe /T
```

### "ffmpeg not found"
```bash
# Windows (Chocolatey):
choco install ffmpeg

# macOS:
brew install ffmpeg

# Linux:
sudo apt-get install ffmpeg
```

### "Timeout errors during analysis"
Your hardware is slower. Increase timeout:
```bash
export DDH_OLLAMA_TIMEOUT=600  # 10 minutes
ddh analyze /path/to/files
```

### "Memory errors with large files"
Process in batches:
```bash
ddh scan /path/to/files --batch-size 100
```

---

## 📝 Recent Improvements

### v0.3.0
- ✅ **Folder organization** — Automatic move proposals with deduplication
- ✅ **Stem word deduplication** — Removes description echoes from AI-generated names
- ✅ **Normalized folder names** — Consistent space-to-underscore conversion
- ✅ **Improved timeout handling** — Configurable per-request timeout for slow hardware
- ✅ **Database reliability** — SQLite WAL mode prevents lock contention
- ✅ **Vision model detection** — Auto-detects gemma4, gemma3-vision, LLaVA variants
- ✅ **Automatic proposal execution** — Execute with default confidence 0.5 for trusted runs

---

## 📚 Documentation

- **[Installation Guide](INSTALL.md)** — Detailed setup for all platforms
- **[GitHub Wiki](https://github.com/Yariminal/DoneDataHoarder/wiki)** — Advanced topics
- **[Issues & Discussions](https://github.com/Yariminal/DoneDataHoarder/issues)** — Community support

---

## 🤝 Contributing

Contributions welcome! To get started:

```bash
git clone https://github.com/Yariminal/DoneDataHoarder.git
cd DoneDataHoarder
pip install -e ".[dev,all]"
pytest tests/
```

---

## 📄 License

MIT License — see [LICENSE](LICENSE) file for details.

---

## 🙋 Support

- **GitHub Issues** — Bug reports and feature requests
- **GitHub Discussions** — Questions and ideas
- **Email** — Contact the maintainer

---

## 🎓 Learn More

- **How LLM-based file organization works** — See `donedatahoarder/proposals/namer.py` for the AI renaming logic
- **Database architecture** — Explore `donedatahoarder/db/models.py`
- **Pipeline orchestration** — Check `donedatahoarder/cli.py` for command flow

Enjoy organizing your data! 🎉

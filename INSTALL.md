# DataHoarder Installation Guide

## Quick Start (No Video Analysis)

DataHoarder works great for organizing images, documents, and metadata extraction without any additional dependencies:

```bash
pip install datahoarder
datahoarder serve --port 8000
```

## Full Installation (Including Video Analysis)

For video and audio frame extraction + transcription, you'll need ffmpeg and optional audio processing:

### Windows

```powershell
# Install ffmpeg via Chocolatey (requires admin)
choco install ffmpeg -y

# Or download from: https://ffmpeg.org/download.html
# Then add ffmpeg to your PATH
```

### macOS

```bash
brew install ffmpeg
```

### Linux (Ubuntu/Debian)

```bash
sudo apt-get update
sudo apt-get install ffmpeg
```

## Optional Python Dependencies

The following extras are automatically gracefully degraded if missing:

| Feature | Package | Install | Impact if Missing |
|---------|---------|---------|-------------------|
| Video frame extraction | ffmpeg-python | `pip install ffmpeg-python` | Videos analyzed with transcript only, no visual frame analysis |
| Audio transcription | faster-whisper | `pip install faster-whisper` | Audio analyzed without transcript, text-based only |
| PDF documents | pdfplumber | `pip install pdfplumber` | PDF metadata extracted, text content skipped |
| Office docs (.docx) | python-docx | `pip install python-docx` | Basic file analysis only |
| Excel spreadsheets | openpyxl | `pip install openpyxl` | Basic file analysis only |

## Installation by Feature

### Image & Document Organization (Minimum)
```bash
pip install datahoarder
# Works great for: JPG, PNG, GIF, WEBP, PDF, DOCX, XLSX
```

### + Video Support
```bash
pip install ffmpeg-python
# Also requires: ffmpeg binary (see OS-specific instructions above)
```

### + Audio Transcription
```bash
pip install faster-whisper
# For: MP3, M4A, WAV, FLAC, OGG, etc.
# Includes automatic speech-to-text
```

### Full Features (Everything)
```bash
pip install datahoarder[all]
# Or install individual components as needed
```

## Verifying Installation

Check what's available:

```bash
datahoarder --help
```

When you run analysis, DataHoarder will:
- ✅ Use installed tools for optimal analysis
- ✅ Gracefully degrade if optional dependencies are missing
- ✅ Annotate analysis results with what was available
- ✅ Reduce confidence scores for partial analyses

Example feedback in your session:

```
Video with ffmpeg available:
  ✓ Frame extraction (4 keyframes analyzed)
  ✓ Transcript available
  → High confidence

Video without ffmpeg:
  ⚠ Frame extraction unavailable
  ✓ Transcript available
  → Moderate confidence (visual cues missed)

Audio without faster-whisper:
  ⚠ Transcription unavailable
  ✓ Basic metadata analysis
  → Lower confidence (speech content missed)
```

## Troubleshooting

### "ffmpeg not found in PATH"
- **Windows**: Add ffmpeg installation folder to PATH, or reinstall via Chocolatey
- **Mac/Linux**: Verify with `which ffmpeg`, or reinstall via package manager

### "ffmpeg-python import error"
```bash
pip install --upgrade ffmpeg-python
```

### "faster-whisper taking too long"
First run downloads the model (~1.5GB). Subsequent runs are fast.
```bash
# Pre-download model:
python -c "from faster_whisper import WhisperModel; WhisperModel('base')"
```

## Docker (Production)

If you prefer a containerized environment:

```bash
docker build -t datahoarder .
docker run -p 8000:8000 -v /path/to/files:/data datahoarder serve
```

(See Dockerfile in repo)

## Development

For contributing:

```bash
git clone https://github.com/Yariminal/DoneDataHoarder.git
cd DoneDataHoarder
pip install -e ".[dev]"
pip install -e ".[all]"  # Optional: all extras for full feature testing
```

Development dependencies are listed in `requirements-dev.txt` (lightweight wrapper
around `pyproject.toml` extras).

"""
Video analyzer — samples frames + optional audio transcription.

This analyzer gracefully handles missing dependencies:

✓ With ffmpeg-python + ffmpeg binary:
  - Extracts 4 keyframes from videos (2%, 45%, 78%, 94%)
  - Uses vision model for frame analysis

✓ With faster-whisper:
  - Transcribes audio/video speech content
  - Provides speech-to-text for analysis

⚠ Without optional dependencies:
  - Videos analyzed with transcript only (text-based)
  - Audio analyzed with basic metadata (no transcription)
  - Confidence scores reduced to reflect incomplete analysis

Installation:
  Optional: pip install ffmpeg-python  (also requires ffmpeg binary)
  Optional: pip install faster-whisper

See INSTALL.md for platform-specific ffmpeg setup instructions.
"""
import io
import subprocess
from pathlib import Path
from typing import Optional

from datahoarder.analyzers.base import AnalysisResult, BaseAnalyzer, SYSTEM_PROMPT
from datahoarder.db.models import File

try:
    from PIL import Image as PilImage
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

try:
    import ffmpeg as _ffmpeg
    _HAS_FFMPEG = True
except ImportError:
    _HAS_FFMPEG = False

try:
    from faster_whisper import WhisperModel
    _HAS_WHISPER = True
except ImportError:
    _HAS_WHISPER = False

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm",
    ".m4v", ".3gp", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg",
}
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".wma", ".opus",
}

# Sample frames at these percentages of the video duration.
# Chosen to capture beginning context, two mid-points, and near-end.
FRAME_POSITIONS = [0.02, 0.45, 0.78, 0.94]
MAX_SIDE = 768            # resize frames before sending to vision model
WHISPER_MODEL = "base"    # base / small / medium / large
TRANSCRIPT_MAX_CHARS = 1500


def _get_duration_seconds(path: Path) -> Optional[float]:
    """Use ffprobe to get video duration."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        import json
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0)) or None
    except Exception:
        return None


def _extract_frame(path: Path, timestamp: float) -> Optional[bytes]:
    """Extract a single frame at *timestamp* seconds, return JPEG bytes."""
    if not _HAS_FFMPEG:
        return None
    try:
        # Use subprocess directly to allow timeout, as ffmpeg-python .run() has no timeout
        cmd = [
            "ffmpeg", "-y", "-ss", str(timestamp), "-i", str(path),
            "-vframes", "1", "-f", "image2", "-vcodec", "mjpeg", "-"
        ]
        result = subprocess.run(
            cmd, capture_output=True, timeout=30
        )
        out = result.stdout
        if not out:
            return None
        if _HAS_PIL:
            with PilImage.open(io.BytesIO(out)) as img:
                img = img.convert("RGB")
                w, h = img.size
                if max(w, h) > MAX_SIDE:
                    ratio = MAX_SIDE / max(w, h)
                    img = img.resize((int(w * ratio), int(h * ratio)))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=80)
                return buf.getvalue()
        return out
    except Exception:
        return None


def _transcribe(path: Path, model_size: str = WHISPER_MODEL) -> str:
    """Transcribe audio/video with faster-whisper, return text excerpt."""
    if not _HAS_WHISPER:
        return ""
    try:
        model = WhisperModel(model_size, device="auto", compute_type="auto")
        segments, _ = model.transcribe(str(path), beam_size=1, language=None)
        parts = []
        chars = 0
        for seg in segments:
            parts.append(seg.text.strip())
            chars += len(seg.text)
            if chars >= TRANSCRIPT_MAX_CHARS:
                break
        return " ".join(parts)[:TRANSCRIPT_MAX_CHARS]
    except Exception:
        return ""


VIDEO_PROMPT = """\
Analyze these video frames to understand what the video is about.

Context about the file:
{context}

{transcript_section}

I've sampled {frame_count} frames from the video. Describe what you observe across all frames.

Return a JSON object:
{{
  "description": "2-3 sentences describing the video content",
  "suggested_name": "meaningful filename stem (no extension, no date prefix, use_underscores, max 60 chars)",
  "tags": ["tag1", "tag2", ...],
  "video_type": "one of: home_video, event, tutorial, presentation, screen_recording, movie_clip, music_video, other",
  "detected_date": "YYYY-MM-DD if inferable, else null",
  "confidence": 0.0-1.0
}}
"""

AUDIO_PROMPT = """\
Analyze this audio file to understand what it contains.

Context about the file:
{context}

{transcript_section}

Return a JSON object:
{{
  "description": "1-2 sentences describing the audio content",
  "suggested_name": "meaningful filename stem (no extension, use_underscores, max 60 chars)",
  "tags": ["tag1", "tag2", ...],
  "audio_type": "one of: music, podcast, voice_memo, lecture, meeting_recording, sound_effect, other",
  "detected_date": "YYYY-MM-DD if inferable, else null",
  "confidence": 0.0-1.0
}}
"""


class VideoAnalyzer(BaseAnalyzer):
    def __init__(self, ai_client, whisper_model: str = WHISPER_MODEL):
        self._client = ai_client
        self._whisper_model = whisper_model

    def can_handle(self, mime_type: str, extension: str) -> bool:
        ext = extension.lower()
        is_video = bool(ext in VIDEO_EXTENSIONS or (mime_type and mime_type.startswith("video/")))
        is_audio = bool(ext in AUDIO_EXTENSIONS or (mime_type and mime_type.startswith("audio/")))

        # Handle both video and audio files.
        # Videos without ffmpeg will be analyzed with transcript only (text-based).
        # Audio files with whisper will be transcribed; without it, basic analysis only.
        return is_video or is_audio

    def analyze(self, file_rec: File, context: str) -> AnalysisResult:
        path = Path(file_rec.path)
        ext = path.suffix.lower()
        is_audio_only = ext in AUDIO_EXTENSIONS or (
            file_rec.mime_type and file_rec.mime_type.startswith("audio/")
        )

        transcript = _transcribe(path, self._whisper_model)
        transcript_section = (
            f"Audio transcript excerpt:\n---\n{transcript}\n---"
            if transcript
            else "No transcript available."
        )

        if is_audio_only:
            whisper_note = ""
            if not _HAS_WHISPER:
                whisper_note = (
                    "\n⚠ faster-whisper not installed: audio transcription unavailable. "
                    "Install for better audio analysis: pip install faster-whisper"
                )
                transcript_section = "No transcript available (whisper not installed)."

            prompt = AUDIO_PROMPT.format(
                context=context,
                transcript_section=transcript_section,
            )
            if whisper_note:
                prompt += whisper_note

            try:
                data = self._client.generate_json(prompt, system=SYSTEM_PROMPT)
            except Exception as exc:
                return AnalysisResult(
                    description=f"AI inference failed: {exc}",
                    transcript=transcript,
                    confidence=0.0,
                )
            result = AnalysisResult.from_ai_response(data)
            result.transcript = transcript

            # Lower confidence slightly if critical tools are missing
            if not _HAS_WHISPER:
                result.confidence = max(0.0, result.confidence - 0.15)

            audio_type = data.get("audio_type", "")
            if audio_type and audio_type not in result.tags:
                result.tags.insert(0, audio_type)
            return result

        # --- Video: extract frames at 2%, 45%, 78%, 94% of duration ---
        frames: list[bytes] = []
        ffmpeg_warning = ""

        if not _HAS_FFMPEG:
            # ffmpeg not available — can't extract frames, but still analyze with transcript
            ffmpeg_warning = (
                "\n⚠ ffmpeg not installed: video frame extraction unavailable. "
                "Analysis based on transcript/metadata only. "
                "Install ffmpeg for full video analysis: https://ffmpeg.org/download.html"
            )
        else:
            # Try to extract frames
            duration = _get_duration_seconds(path)
            if duration and duration > 0:
                for pct in FRAME_POSITIONS:
                    ts = duration * pct
                    frame = _extract_frame(path, ts)
                    if frame:
                        frames.append(frame)

        pct_labels = ", ".join(f"{int(p*100)}%" for p in FRAME_POSITIONS[:len(frames)])
        prompt = VIDEO_PROMPT.format(
            context=context,
            transcript_section=transcript_section,
            frame_count=len(frames),
        )
        if frames:
            prompt += f"\nFrames sampled at: {pct_labels} of the video duration."
        if ffmpeg_warning:
            prompt += ffmpeg_warning

        try:
            if frames:
                # Send all sampled frames to the vision model
                data = self._client.generate_json(
                    prompt,
                    images_list=frames,
                    system=SYSTEM_PROMPT,
                )
            else:
                # No frames — text-only with transcript
                data = self._client.generate_json(prompt, system=SYSTEM_PROMPT)
        except Exception as exc:
            return AnalysisResult(
                description=f"AI inference failed: {exc}",
                transcript=transcript,
                confidence=0.0,
            )

        result = AnalysisResult.from_ai_response(data)
        result.transcript = transcript

        # Lower confidence if critical tools are missing
        if not _HAS_FFMPEG:
            result.confidence = max(0.0, result.confidence - 0.20)
        if not _HAS_WHISPER:
            result.confidence = max(0.0, result.confidence - 0.10)

        video_type = data.get("video_type", "")
        if video_type and video_type not in result.tags:
            result.tags.insert(0, video_type)
        return result

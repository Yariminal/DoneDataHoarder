"""
Ollama management endpoints (status, models, pull, start/restart).
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from starlette.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter()

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Recommended models for DoneDataHoarder
# Latest versions first: Gemma 4 is now available on Ollama!
RECOMMENDED_MODELS = [
    # Gemma 4 (Latest from Google - NOW AVAILABLE on Ollama)
    {"name": "gemma4:31b",  "desc": "Gemma 4 31B - Highest quality, dense architecture, multimodal, 256K context", "size": "20 GB", "vision": True, "latest": True},
    {"name": "gemma4:26b",  "desc": "Gemma 4 26B - Mixture of Experts, balanced quality/speed, multimodal, 256K context", "size": "18 GB", "vision": True, "latest": True},
    {"name": "gemma4:e4b",  "desc": "Gemma 4 E4B - Edge variant, multimodal+audio, 128K context", "size": "9.6 GB", "vision": True, "latest": True},
    {"name": "gemma4:e2b",  "desc": "Gemma 4 E2B - Lightweight edge variant, multimodal+audio, 128K context", "size": "7.2 GB", "vision": True, "latest": True},
    # Gemma 2 (stable, proven quality)
    {"name": "gemma2:27b",  "desc": "Gemma 2 27B - High quality, multimodal, needs 20GB+ RAM", "size": "16 GB", "vision": True},
    {"name": "gemma2:9b",   "desc": "Gemma 2 9B - Best balance of quality and speed", "size": "5.5 GB", "vision": True},
    # Gemma 3 (solid performers)
    {"name": "gemma3:12b",  "desc": "Gemma 3 12B - Good quality, multimodal", "size": "8.1 GB", "vision": True},
    {"name": "gemma3:4b",   "desc": "Gemma 3 4B - Fast and lightweight, multimodal", "size": "3.3 GB", "vision": True},
    # Vision specialists
    {"name": "llava:13b",   "desc": "LLaVA 13B - Specialized vision model", "size": "8.0 GB", "vision": True},
    {"name": "llava:7b",    "desc": "LLaVA 7B - Lightweight vision model", "size": "4.7 GB", "vision": True},
    # Lightweight text-only
    {"name": "llama3.2:3b", "desc": "Llama 3.2 3B - Fast text-only, only 2GB", "size": "2.0 GB", "vision": False},
]


@router.get("/ollama/status")
def ollama_status():
    """Check if Ollama is installed and running."""
    # Check if ollama binary exists
    ollama_path = shutil.which("ollama")
    installed = ollama_path is not None

    # Check if server is reachable
    running = False
    version = None
    if installed:
        try:
            resp = httpx.get(f"{OLLAMA_HOST}/api/version", timeout=3)
            if resp.status_code == 200:
                running = True
                version = resp.json().get("version")
        except Exception:
            pass

    return {
        "installed": installed,
        "running": running,
        "version": version,
        "ollama_path": ollama_path,
        "host": OLLAMA_HOST,
        "download_url": "https://ollama.com/download",
    }


@router.get("/ollama/models")
def list_ollama_models():
    """List locally installed Ollama models."""
    try:
        resp = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        resp.raise_for_status()
        models = resp.json().get("models", [])
    except Exception:
        return {"models": [], "error": "Cannot reach Ollama server"}

    items = []
    for m in models:
        name = m.get("name", "")
        size = m.get("size", 0)
        # Check if this is a vision-capable model
        is_vision = any(v in name.lower() for v in ("llava", "gemma3", "gemma4", "bakllava", "moondream"))
        items.append({
            "name": name,
            "size_bytes": size,
            "modified_at": m.get("modified_at"),
            "vision": is_vision,
            "family": m.get("details", {}).get("family", ""),
            "parameters": m.get("details", {}).get("parameter_size", ""),
        })

    return {"models": items, "recommended": RECOMMENDED_MODELS}


class PullModelRequest(BaseModel):
    model: str


@router.post("/ollama/pull")
def pull_ollama_model(body: PullModelRequest):
    """
    Pull (download) an Ollama model with streaming progress updates.
    Returns Server-Sent Events (SSE) with progress information.
    """
    model = body.model.strip()
    if not model:
        raise HTTPException(400, "Model name required")

    def pull_stream():
        try:
            # Use separate connect/read timeouts: 30s to connect, 1 hour for reads
            # (large models can take a long time to download)
            timeout = httpx.Timeout(connect=30.0, read=3600.0, write=30.0, pool=30.0)
            with httpx.stream(
                "POST",
                f"{OLLAMA_HOST}/api/pull",
                json={"name": model, "stream": True},
                timeout=timeout,
            ) as resp:
                if resp.status_code != 200:
                    yield f"data: {json.dumps({'status': 'error', 'message': f'Ollama API returned {resp.status_code}'})}\n\n"
                    return

                # Track the largest layer to compute overall progress
                largest_total = 0
                largest_completed = 0
                pull_succeeded = False

                for line in resp.iter_lines():
                    if line:
                        try:
                            data = json.loads(line)
                            status = data.get("status", "")
                            total = data.get("total", 0)
                            completed = data.get("completed", 0)

                            # Track progress of the largest layer (the actual model weights)
                            if total > largest_total:
                                largest_total = total
                                largest_completed = completed
                            elif total == largest_total and total > 0:
                                largest_completed = completed

                            # Calculate progress based on largest layer
                            if largest_total > 0:
                                progress = min(99, int((largest_completed / largest_total * 100)))
                            else:
                                progress = 0

                            # Detect successful completion from Ollama
                            if status == "success":
                                pull_succeeded = True

                            yield f"data: {json.dumps({'status': status, 'progress': progress, 'completed': largest_completed, 'total': largest_total})}\n\n"
                        except json.JSONDecodeError:
                            pass

                # Only send success if Ollama actually reported success
                if pull_succeeded:
                    yield f"data: {json.dumps({'status': 'success', 'progress': 100})}\n\n"
                else:
                    # Verify by checking if model exists
                    try:
                        check = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
                        models = [m.get("name", "") for m in check.json().get("models", [])]
                        if any(model in m or m in model for m in models):
                            yield f"data: {json.dumps({'status': 'success', 'progress': 100})}\n\n"
                        else:
                            yield f"data: {json.dumps({'status': 'error', 'message': 'Download stream ended but model not found in Ollama.'})}\n\n"
                    except Exception:
                        yield f"data: {json.dumps({'status': 'success', 'progress': 100})}\n\n"
        except httpx.TimeoutException:
            yield f"data: {json.dumps({'status': 'error', 'message': 'Pull timed out. Model may still be downloading. Check Ollama status with: ollama list'})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'status': 'error', 'message': f'Pull failed: {str(exc)}'})}\n\n"

    return StreamingResponse(pull_stream(), media_type="text/event-stream")


@router.post("/ollama/delete")
def delete_ollama_model(body: PullModelRequest):
    """Delete an Ollama model via POST request."""
    model = body.model.strip()
    if not model:
        raise HTTPException(400, "Model name required")
    try:
        resp = httpx.request(
            "DELETE",
            f"{OLLAMA_HOST}/api/delete",
            json={"name": model},
            timeout=30,
        )
        resp.raise_for_status()
        return {"status": "deleted", "model": model}
    except Exception as exc:
        raise HTTPException(500, f"Delete failed: {exc}")


class StartOllamaRequest(BaseModel):
    num_parallel: Optional[int] = None


def _start_ollama_process(ollama_path: str, num_parallel: int | None = None):
    """Start the Ollama server process with optional OLLAMA_NUM_PARALLEL."""
    env = os.environ.copy()
    if num_parallel and num_parallel > 1:
        env["OLLAMA_NUM_PARALLEL"] = str(num_parallel)

    if platform.system() == "Windows":
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        detached_process = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        subprocess.Popen(
            [ollama_path, "serve"],
            env=env,
            creationflags=create_no_window | detached_process,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            [ollama_path, "serve"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


@router.post("/ollama/start")
def start_ollama(body: StartOllamaRequest = StartOllamaRequest()):
    """Attempt to start the Ollama server."""
    ollama_path = shutil.which("ollama")
    if not ollama_path:
        raise HTTPException(404, "Ollama not found. Install from https://ollama.com/download")

    try:
        _start_ollama_process(ollama_path, body.num_parallel)

        # Give it a moment to start
        import time
        time.sleep(2)

        # Verify it started
        try:
            resp = httpx.get(f"{OLLAMA_HOST}/api/version", timeout=3)
            if resp.status_code == 200:
                return {"status": "started", "version": resp.json().get("version")}
        except Exception:
            pass

        return {"status": "starting", "message": "Ollama is starting up..."}
    except Exception as exc:
        raise HTTPException(500, f"Failed to start Ollama: {exc}")


@router.post("/ollama/restart")
def restart_ollama(body: StartOllamaRequest = StartOllamaRequest()):
    """Stop and restart Ollama with updated settings (e.g. OLLAMA_NUM_PARALLEL)."""
    ollama_path = shutil.which("ollama")
    if not ollama_path:
        raise HTTPException(404, "Ollama not found. Install from https://ollama.com/download")

    import time

    # Stop the running Ollama process
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/f", "/im", "ollama.exe"],
                           capture_output=True, timeout=10)
            # Also kill the runner process
            subprocess.run(["taskkill", "/f", "/im", "ollama_llama_server.exe"],
                           capture_output=True, timeout=10)
        else:
            subprocess.run(["pkill", "-f", "ollama serve"],
                           capture_output=True, timeout=10)
    except Exception:
        pass  # Process may not be running

    time.sleep(1)

    # Start with new settings
    try:
        _start_ollama_process(ollama_path, body.num_parallel)
        time.sleep(3)

        # Verify it started
        try:
            resp = httpx.get(f"{OLLAMA_HOST}/api/version", timeout=5)
            if resp.status_code == 200:
                return {
                    "status": "restarted",
                    "version": resp.json().get("version"),
                    "num_parallel": body.num_parallel,
                }
        except Exception:
            pass

        return {"status": "restarting", "message": "Ollama is restarting..."}
    except Exception as exc:
        raise HTTPException(500, f"Failed to restart Ollama: {exc}")

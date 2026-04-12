#!/usr/bin/env python3
"""
Test script for pipeline with 54-file folder.
"""
import json
import time
import requests
import subprocess
import sys
from pathlib import Path

TEST_FOLDER = r"D:\Work Projects\YAELI PROJECTS\YAELI PROJECTS\MODY 2020\DAN"
BASE_URL = "http://127.0.0.1:8080/api"

def start_server():
    """Start the DataHoarder server in background."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "datahoarder.cli", "serve"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(3)  # Wait for server to start
    return proc

def create_session(folder_path: str):
    """Create a new analysis session."""
    payload = {
        "root_path": folder_path,
        "backend": "ollama",
        "model": "gemma4:e4b",
        "workers": 8,
        "preferred_language": "english",
    }
    resp = requests.post(f"{BASE_URL}/sessions", json=payload)
    print(f"Create session response: {resp.status_code}")
    resp.raise_for_status()
    return resp.json()

def run_pipeline_step(session_id: str, step: str, root_path: str = ""):
    """Run a pipeline step and wait for completion."""
    print("\n" + "="*60)
    print(f"Running {step}...")
    print("="*60)

    payload = {"session_id": session_id}
    if root_path:
        payload["root_path"] = root_path

    resp = requests.post(f"{BASE_URL}/pipeline/{step}", json=payload)
    print(f"Step response: {resp.status_code}")
    if resp.status_code >= 400:
        print(f"ERROR: {resp.text}")
    resp.raise_for_status()
    result = resp.json()

    if "job_id" in result:
        # This is a long-running job, wait for SSE updates
        job_id = result["job_id"]
        return wait_for_job(job_id)
    else:
        # Synchronous result
        print(json.dumps(result, indent=2))
        return result

def wait_for_job(job_id: str):
    """Wait for a job to complete using SSE."""
    print(f"Job ID: {job_id}")

    # Use polling instead of SSE
    return poll_for_job(job_id)

def poll_for_job(job_id: str, max_wait: int = 900):
    """Poll for job status."""
    start = time.time()
    last_progress = None
    stuck_count = 0

    while time.time() - start < max_wait:
        try:
            resp = requests.get(f"{BASE_URL}/pipeline/jobs/{job_id}")
            if resp.status_code == 200:
                job_info = resp.json()
                progress = job_info.get('progress', {})

                if progress != last_progress:
                    analyzed = progress.get('analyzed', 0)
                    skipped = progress.get('skipped', 0)
                    errors = progress.get('errors', 0)
                    current = progress.get('current', 0)
                    total = progress.get('total', 0)
                    print(f"Progress: {current}/{total} "
                          f"(analyzed={analyzed} skipped={skipped} errors={errors})")
                    last_progress = progress
                    stuck_count = 0
                else:
                    stuck_count += 1
                    if stuck_count > 10:
                        print(f"[WARN] No progress for {stuck_count * 2} seconds...")

                if job_info.get('state') in ['completed', 'failed', 'cancelled']:
                    print(f"[{job_info.get('state').upper()}]")
                    print(json.dumps(progress, indent=2))
                    return progress
        except Exception as e:
            print(f"Poll error: {e}")

        time.sleep(2)

    print("Timeout waiting for job")
    return None

def main():
    server_proc = None
    try:
        print("Starting DataHoarder server...")
        server_proc = start_server()

        # Verify server is running
        for i in range(10):
            try:
                resp = requests.get(f"{BASE_URL}/sessions")
                if resp.status_code == 200:
                    print("[OK] Server is running")
                    break
            except Exception as e:
                print(f"Wait {i+1}/10: {e}")
                time.sleep(1)

        print(f"\nTest Folder: {TEST_FOLDER}")
        print(f"Model: gemma4:e4b")
        print(f"Workers: 8")

        # Create session
        print("\n" + "="*60)
        print("Creating session...")
        print("="*60)
        session = create_session(TEST_FOLDER)
        session_id = session["id"]
        print(f"[OK] Session created: {session_id}")

        # Run steps
        steps = ["scan", "enrich", "dedup", "analyze"]
        step_results = {}
        for i, step in enumerate(steps):
            result = run_pipeline_step(session_id, step, root_path=TEST_FOLDER if step == "scan" else "")
            step_results[step] = result

            if step == "analyze" and result:
                print(f"\n[ANALYZE RESULTS]")
                print(f"   Analyzed: {result.get('analyzed', 0)}")
                print(f"   Skipped: {result.get('skipped', 0)}")
                print(f"   Errors: {result.get('errors', 0)}")
                print(f"   Total: {result.get('total', 0)}")

                # Check if all files were processed
                total = result.get('total', 0)
                processed = result.get('analyzed', 0) + result.get('skipped', 0) + result.get('errors', 0)
                if processed < total:
                    print(f"[ALERT] Only {processed}/{total} files processed!")
                else:
                    print(f"[SUCCESS] All {processed}/{total} files processed!")

        # Summary
        print("\n" + "="*60)
        print("TEST SUMMARY")
        print("="*60)
        for step, result in step_results.items():
            if result:
                if isinstance(result, dict) and 'analyzed' in result:
                    print(f"{step:10s}: {result.get('analyzed', 0):3d} analyzed, "
                          f"{result.get('skipped', 0):3d} skipped, "
                          f"{result.get('errors', 0):3d} errors")
                else:
                    print(f"{step:10s}: {result}")

    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()

    finally:
        if server_proc:
            print("\n\nStopping server...")
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
                print("[OK] Server stopped")
            except subprocess.TimeoutExpired:
                server_proc.kill()
                print("[WARN] Server killed")

if __name__ == "__main__":
    main()

"""
DoneDataHoarder CLI — entry point for all commands.

Usage:
    ddh scan     /path/to/drive
    ddh enrich
    ddh analyze  [--workers N] [--limit N]
    ddh dedup
    ddh propose
    ddh review
    ddh execute  [--commit]
    ddh stats
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Force UTF-8 output on Windows (Hebrew/other non-Latin codepages break Rich spinners)
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

app = typer.Typer(
    name="donedatahoarder",
    help="AI-powered file organization for data hoarders.",
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()


def _print_duplicate_coverage(stage: str, result: dict) -> None:
    """Make bounded candidate search visible without inventing a pair count."""
    if result.get("candidate_coverage") != "bounded_incomplete":
        return
    deferred = result.get("candidate_pair_opportunities_deferred")
    if deferred is None:
        lower = result.get("candidate_pair_opportunities_deferred_lower_bound")
        detail = (f"at least {lower:,}; exact count unknown" if lower
                  else "exact deferred count unknown")
    else:
        detail = f"{deferred:,} pair opportunities deferred"
    console.print(
        f"[yellow]{stage} candidate search was capped; coverage is incomplete "
        f"({detail}). Unseen pairs have not been cleared.[/yellow]"
    )


@app.callback()
def main_callback(
    ctx: typer.Context,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging to console.", envvar="DDH_LOG_VERBOSE")] = False,
):
    """Global options for all DoneDataHoarder commands."""
    # Diagnostics can be piped as JSON, including under --verbose / DDH_LOG.
    # Its capability probe needs neither application logging nor a welcome.
    if ctx.invoked_subcommand == "tui-diagnostics":
        return
    from donedatahoarder.logging import setup_logging
    setup_logging(verbose=verbose)

    # First-run welcome
    _maybe_show_welcome()


def _maybe_show_welcome() -> None:
    """Show a friendly welcome on first run."""
    welcome_file = Path.home() / ".datahoarder" / ".welcome_shown"
    if welcome_file.exists():
        return
    console.print(
        Panel(
            "[bold green]Welcome to DoneDataHoarder![/bold green]\n\n"
            "Your AI-powered file organization assistant.\n"
            "  • Run [cyan]ddh doctor[/cyan] to check your setup\n"
            "  • Run [cyan]ddh scan /path/to/files[/cyan] to get started\n"
            "  • Docs: [blue]https://github.com/Yariminal/DoneDoneDataHoarder[/blue]",
            title="🗄️  DoneDataHoarder",
            style="green",
        )
    )
    try:
        welcome_file.parent.mkdir(parents=True, exist_ok=True)
        welcome_file.touch()
    except OSError:
        pass


def _init_db(db_path: str) -> Path:
    from donedatahoarder.db.session import init_db
    p = Path(db_path)
    init_db(p)
    return p


def _init_ai(backend: str, ollama_host: str, model: str) -> None:
    from donedatahoarder.ai.router import init_ai
    init_ai(
        backend=backend,
        ollama_host=ollama_host,
        text_model=model,
        vision_model=model,
    )


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

@app.command()
def doctor(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
    ollama_host: Annotated[str, typer.Option("--ollama-host", help="Ollama server URL.", envvar="OLLAMA_HOST")] = "http://localhost:11434",
    model: Annotated[str, typer.Option("--model", help="Ollama model name to check.", envvar="DDH_MODEL")] = "gemma3:12b",
    backend: Annotated[str, typer.Option("--backend", help="AI backend: ollama|gemini|auto", envvar="DDH_BACKEND")] = "ollama",
):
    """[bold green]Diagnose[/bold green] the environment: Ollama, disk space, and DB integrity."""
    import shutil
    import sqlite3

    from donedatahoarder.ai.ollama_client import OllamaClient

    console.print(Panel("Running diagnostics…", style="green"))
    table = Table(title="Doctor Report", show_lines=True)
    table.add_column("Check", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Detail", style="dim")

    # --- Ollama reachability ---
    ollama = OllamaClient(host=ollama_host, text_model=model, vision_model=model)
    if ollama.is_available():
        models = ollama.list_models()
        table.add_row("Ollama reachability", "[green]OK[/green]", f"Reachable at {ollama_host}")
        if models:
            # Show installed models
            if len(models) <= 3:
                models_str = ", ".join(models)
            else:
                models_str = f"{len(models)} models installed: {', '.join(models[:3])}…"
            table.add_row("Ollama models", "[green]OK[/green]", f"Installed: {models_str}")
            # If default model not installed, note it's optional
            if model not in models:
                table.add_row(
                    "Default model",
                    "[dim]INFO[/dim]",
                    f"'{model}' not installed (optional). Install with: [bold]ollama pull {model}[/bold]",
                )
        else:
            table.add_row(
                "Ollama models",
                "[yellow]NONE[/yellow]",
                f"No models installed. Get started: [bold]ollama pull {model}[/bold]",
            )
    else:
        table.add_row(
            "Ollama reachability",
            "[red]FAIL[/red]",
            f"Not reachable at {ollama_host}. Start with: [bold]ollama serve[/bold]",
        )

    # --- Gemini (if configured) ---
    if backend in ("gemini", "auto") or os.environ.get("GEMINI_API_KEY"):
        try:
            from donedatahoarder.ai.gemini_client import GeminiClient
            GeminiClient()
            table.add_row("Gemini backend", "[green]OK[/green]", "API key configured")
        except Exception as exc:
            table.add_row("Gemini backend", "[yellow]WARN[/yellow]", str(exc))
    else:
        table.add_row("Gemini backend", "[dim]SKIP[/dim]", "Not configured")

    # --- Disk space ---
    db_path = Path(db)
    try:
        usage = shutil.disk_usage(db_path.resolve().parent)
        free_gb = usage.free / 1024 ** 3
        total_gb = usage.total / 1024 ** 3
        if free_gb < 1.0:
            table.add_row(
                "Disk space",
                "[red]LOW[/red]",
                f"{free_gb:.1f} GB free of {total_gb:.1f} GB",
            )
        else:
            table.add_row(
                "Disk space",
                "[green]OK[/green]",
                f"{free_gb:.1f} GB free of {total_gb:.1f} GB",
            )
    except Exception as exc:
        table.add_row("Disk space", "[yellow]WARN[/yellow]", str(exc))

    # --- DB integrity ---
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path.resolve()), timeout=5)
            cur = conn.execute("PRAGMA integrity_check")
            result = cur.fetchone()[0]
            conn.close()
            if result == "ok":
                table.add_row("DB integrity", "[green]OK[/green]", str(db_path.resolve()))
            else:
                table.add_row("DB integrity", "[red]FAIL[/red]", result)
        except Exception as exc:
            table.add_row("DB integrity", "[red]FAIL[/red]", str(exc))
    else:
        table.add_row("DB integrity", "[yellow]SKIP[/yellow]", "Database does not exist yet")

    console.print(table)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

@app.command()
def scan(
    root: Annotated[Path, typer.Argument(help="Directory to scan.")],
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
    force: Annotated[bool, typer.Option("--force", help="Re-scan already-indexed files.")] = False,
    workers: Annotated[int, typer.Option("--workers", "-w", help="Parallel threads for stat collection (DB stays single-threaded).")] = 1,
):
    """[bold cyan]Scan[/bold cyan] a directory and build the file index."""
    _init_db(db)
    root = root.resolve()
    if not root.exists():
        console.print(f"[red]Path does not exist: {root}[/red]")
        raise typer.Exit(1)

    console.print(Panel(f"Scanning [bold]{root}[/bold]", style="cyan"))

    # Create a UserSession so every scanned file has a valid session_id
    from donedatahoarder.db.session import get_engine
    from donedatahoarder.db.models import UserSession, SessionStatus
    from sqlalchemy.orm import Session
    import uuid

    engine = get_engine()
    with Session(engine) as session:
        user_sess = UserSession(
            id=str(uuid.uuid4()),
            name=root.name,
            root_path=str(root),
            status=SessionStatus.ACTIVE,
        )
        session.add(user_sess)
        session.commit()
        session_id = user_sess.id

    from donedatahoarder.core.scanner import scan as do_scan
    counts = do_scan(root, force_rescan=force, workers=workers, session_id=session_id)

    console.print(
        f"\n[bold green]Scan complete[/bold green] — "
        f"[green]{counts['new']}[/green] new, "
        f"[yellow]{counts['skipped']}[/yellow] skipped, "
        f"[red]{counts['errors']}[/red] errors"
    )


# ---------------------------------------------------------------------------
# enrich
# ---------------------------------------------------------------------------

@app.command()
def enrich(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
    limit: Annotated[Optional[int], typer.Option("--limit", help="Max files to process.")] = None,
):
    """[bold green]Enrich[/bold green] scanned files with metadata, hashes, and dates."""
    _init_db(db)
    console.print(Panel("Extracting metadata & hashes", style="green"))

    from donedatahoarder.core.enricher import enrich as do_enrich
    counts = do_enrich(limit=limit)

    console.print(
        f"\n[bold green]Enrichment complete[/bold green] — "
        f"{counts['enriched']} enriched, "
        f"{counts['errors']} errors"
    )


# ---------------------------------------------------------------------------
# refresh-photos
# ---------------------------------------------------------------------------

@app.command("refresh-photos")
def refresh_photos(
    session_id: Annotated[str, typer.Option("--session", help="Existing session to refresh.")],
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
    workers: Annotated[int, typer.Option("--workers", "-w", min=1, max=32)] = 1,
):
    """Refresh indexed photo evidence without resetting analysis or changing photos."""
    _init_db(db)
    from donedatahoarder.core.enricher import refresh_photo_metadata
    try:
        counts = refresh_photo_metadata(session_id=session_id, workers=workers)
    except ValueError as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc
    console.print("Photo evidence refreshed. Existing keepers are preserved; inspect comparisons before approving.")
    for name, count in counts.items():
        console.print(f"{name}: {count}", markup=False)


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

@app.command()
def analyze(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
    backend: Annotated[str, typer.Option("--backend", help="AI backend: ollama|gemini|auto", envvar="DDH_BACKEND")] = "ollama",
    ollama_host: Annotated[str, typer.Option("--ollama-host", help="Ollama server URL.", envvar="OLLAMA_HOST")] = "http://localhost:11434",
    model: Annotated[str, typer.Option("--model", help="Model name (must support vision).", envvar="DDH_MODEL")] = "gemma3:12b",
    session_id: Annotated[Optional[str], typer.Option("--session", help="Analyze only this session.")] = None,
    workers: Annotated[int, typer.Option("--workers", "-w", help="Parallel workers.")] = 1,
    limit: Annotated[Optional[int], typer.Option("--limit", help="Max files to analyze.")] = None,
    min_size: Annotated[int, typer.Option("--min-size", help="Skip files smaller than N KB.")] = 1,
    retry_errors: Annotated[bool, typer.Option("--retry-errors", help="Retry files that failed AI inference in a prior analysis run.")] = False,
    sequence_sample_stride: Annotated[int, typer.Option("--sequence-sample-stride", help="Opt in to analyzing every Nth adjacent numbered image frame; 0 analyzes all.")] = 0,
    use_cache: Annotated[bool, typer.Option("--cache/--no-cache", help="Reuse verified analysis only when bytes, context, model digest and versions match.")] = True,
):
    """[bold magenta]Analyze[/bold magenta] enriched files with AI (vision + text)."""
    _init_db(db)
    console.print(Panel(f"AI analysis — backend: [bold]{backend}[/bold], model: [bold]{model}[/bold]", style="magenta"))

    try:
        _init_ai(backend, ollama_host, model)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    from donedatahoarder.analyzers.pipeline import analyze as do_analyze
    counts = do_analyze(workers=workers, limit=limit, min_size_kb=min_size,
                        session_id=session_id, retry_errors=retry_errors,
                        sequence_sample_stride=sequence_sample_stride,
                        use_cache=use_cache)

    console.print(
        f"\n[bold green]Analysis complete[/bold green] — "
        f"{counts['analyzed']} analyzed, "
        f"{counts.get('cached', 0)} cached, "
        f"{counts.get('sampled', 0)} sampled, "
        f"{counts['skipped']} skipped, "
        f"{counts['errors']} errors"
    )


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------

@app.command()
def dedup(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
    no_perceptual: Annotated[bool, typer.Option("--no-perceptual", help="Skip perceptual image hashing.")] = False,
):
    """[bold yellow]Deduplicate[/bold yellow] — find exact and near-duplicate files."""
    _init_db(db)
    console.print(Panel("Duplicate detection", style="yellow"))

    from donedatahoarder.core.dedup import (
        find_exact_duplicates, find_perceptual_duplicates,
        find_text_near_duplicates, find_semantic_duplicates,
        duplicate_summary, generate_dedup_proposals,
    )

    exact = find_exact_duplicates()
    console.print(
        f"Exact duplicates: [bold]{exact['groups']}[/bold] groups, "
        f"[yellow]{exact['duplicates']}[/yellow] redundant files"
    )

    if not no_perceptual:
        perc = find_perceptual_duplicates()
        if "error" in perc:
            console.print(f"[yellow]Perceptual hashing skipped: {perc['error']}[/yellow]")
        else:
            console.print(
                f"Similar-image candidates: [bold]{perc['groups']}[/bold] groups, "
                f"[yellow]{perc['duplicates']}[/yellow] keeper-relative candidates"
            )
            _print_duplicate_coverage("Perceptual", perc)

    text_matches = find_text_near_duplicates()
    console.print(
        f"Similar-text candidates: [bold]{text_matches['groups']}[/bold] groups, "
        f"[yellow]{text_matches['duplicates']}[/yellow] keeper-relative candidates"
    )
    _print_duplicate_coverage("Text", text_matches)
    semantic = find_semantic_duplicates()
    console.print(
        f"Semantic candidates: [bold]{semantic['groups']}[/bold] groups, "
        f"[yellow]{semantic['duplicates']}[/yellow] keeper-relative candidates"
    )
    _print_duplicate_coverage("Semantic", semantic)

    # Stage 5 — turn detected groups into actionable MARK_DUPLICATE proposals
    prop_counts = generate_dedup_proposals()
    if prop_counts["created"]:
        console.print(
            f"\n[bold green]Created {prop_counts['created']} duplicate review proposals[/bold green] "
            f"({prop_counts['groups']} groups, {prop_counts['skipped']} already existed, "
            f"{prop_counts['no_keeper']} missing keeper)"
        )
        console.print("Run [bold]ddh review --dupes[/bold] to inspect, then [bold]ddh execute --commit[/bold] to clean up.")
    else:
        console.print("\n[dim]No new duplicate proposals created.[/dim]")

    summary = duplicate_summary()
    exact_bytes = sum(g["wasted_bytes"] for g in summary
                      if getattr(g["type"], "value", g["type"]) == "exact")
    if exact_bytes:
        console.print(
            f"\n[bold]Bytes represented by non-keeper exact copies: "
            f"[green]{exact_bytes / 1024 / 1024:.1f} MB[/green][/bold] "
            "[dim](before dependency and individual review)[/dim]"
        )


# ---------------------------------------------------------------------------
# relate
# ---------------------------------------------------------------------------

@app.command()
def relate(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
    session_id: Annotated[Optional[str], typer.Option("--session", help="Session ID to process. Defaults to latest.")] = None,
    scope: Annotated[str, typer.Option("--scope", help="'per_directory' (default) or 'cross_directory'.")] = "per_directory",
    backend: Annotated[str, typer.Option("--backend", help="'ollama' or 'gemini'.")] = "ollama",
    ollama_host: Annotated[str, typer.Option("--ollama-host", help="Ollama server URL.", envvar="OLLAMA_HOST")] = "http://localhost:11434",
    model: Annotated[str, typer.Option("--model", "-m", help="LLM model for this run.")] = "gemma3:12b",
    relate_model: Annotated[str, typer.Option("--relate-model", help="Dedicated model for the relate step (defaults to --model if not set).")] = "gemma4:26b",
):
    """[bold cyan]Relate[/bold cyan] — LLM-group files that are conceptually one thing (CAD + backups + exports, etc.)."""
    _init_db(db)
    # Relate uses its own model (defaults to gemma4:26b for reasoning)
    effective_model = relate_model
    _init_ai(backend, ollama_host, effective_model)
    console.print(Panel("Finding related file groups", style="cyan"))

    from donedatahoarder.core.relate import relate as do_relate
    from donedatahoarder.db.models import UserSession
    from donedatahoarder.db.session import get_engine
    from sqlalchemy.orm import Session as _Session

    # Resolve session
    if not session_id:
        with _Session(get_engine()) as s:
            latest = (
                s.query(UserSession).order_by(UserSession.updated_at.desc()).first()
            )
            if not latest:
                console.print("[red]No sessions found. Run `ddh scan` first.[/red]")
                raise typer.Exit(1)
            session_id = latest.id
            console.print(f"Using latest session: [cyan]{session_id}[/cyan]")

    def _cb(d: dict) -> None:
        progress = (f"{d['done']}/{d['total']}" if d.get("total") is not None
                    else str(d["done"]))
        console.print(
            f"  [dim]{progress}[/dim]  "
            f"[bold]{d['groups']}[/bold] groups so far "
            f"([green]{d['llm_groups']} LLM[/green] + "
            f"[yellow]{d['backstop_groups']} backstop[/yellow])"
        )

    summary = do_relate(
        session_id=session_id, scope=scope, model=effective_model, progress_cb=_cb,
    )
    console.print(
        f"\n[bold green]Relate complete[/bold green] — "
        f"{summary['directories']} dir(s), "
        f"[bold]{summary['groups']}[/bold] groups "
        f"({summary['members']} members, "
        f"[green]{summary['llm_groups']} LLM[/green] + "
        f"[yellow]{summary['backstop_groups']} backstop[/yellow])"
    )


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------

@app.command()
def propose(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
    limit: Annotated[Optional[int], typer.Option("--limit", min=0, help="Max analyzed files to process; omits collection-wide postpasses.")] = None,
    offset: Annotated[Optional[int], typer.Option("--offset", min=0, help="Skip first N files.")] = None,
    no_organize: Annotated[bool, typer.Option("--no-organize", help="Skip folder reorganization proposals.")] = False,
):
    """[bold blue]Generate[/bold blue] rename/tag and folder reorganization proposals from analyzed files."""
    _init_db(db)
    console.print(Panel("Generating proposals", style="blue"))

    # Resolve the latest user session up-front. The Namer's post-pass functions
    # (sibling propagation, useless-stem rescue, hygiene fallback, etc.) all
    # short-circuit when session_id is None — without this, files like `1.jpg`
    # whose AI analysis failed never get a fallback rename proposal generated,
    # leaving them stranded with their original useless stems on disk.
    from donedatahoarder.db.session import get_engine
    from donedatahoarder.db.models import UserSession
    from sqlalchemy.orm import Session
    engine = get_engine()
    with Session(engine) as session:
        latest = session.query(UserSession).order_by(UserSession.updated_at.desc()).first()
        session_id = latest.id if latest else None

    from donedatahoarder.proposals.namer import generate_proposals
    counts = generate_proposals(limit=limit, offset=offset, session_id=session_id)

    console.print(
        f"\n[bold green]Proposals ready[/bold green] — "
        f"{counts['rename']} renames, "
        f"{counts['tags']} tag updates, "
        f"{counts['skipped']} unchanged"
    )

    if limit is not None or offset is not None:
        console.print("Skipping collection-wide organization for this file slice.", markup=False)
    elif not no_organize:
        console.print(Panel("Analyzing folder structure for reorganization…", style="cyan"))
        from donedatahoarder.proposals.organizer import generate_reorg_proposals

        # Initialize AI provider for the organizer (uses LLM for reorg suggestions)
        # Default to a small, fast text model; user can override via --model if needed.
        _init_ai("ollama", "http://localhost:11434", "llama3.2:3b")

        if session_id:
            org_counts = generate_reorg_proposals(session_id=session_id)
            console.print(
                f"[bold cyan]Organization proposals[/bold cyan] — "
                f"{org_counts.get('move', 0)} moves, "
                f"{org_counts.get('rename_folder', 0)} folder renames, "
                f"{org_counts.get('skipped', 0)} skipped"
            )
        else:
            console.print("[yellow]No active session — skipping folder reorganization.[/yellow]")

    console.print("Run [bold]ddh review[/bold] to inspect them.")


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

@app.command()
def review(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
    session_id: Annotated[Optional[str], typer.Option("--session", "-s", help="Limit review to this session.")] = None,
    min_confidence: Annotated[float, typer.Option("--min-confidence", "-c")] = 0.0,
    dupes: Annotated[bool, typer.Option("--dupes", help="Show duplicate groups instead of rename proposals.")] = False,
    interactive: Annotated[bool, typer.Option("--interactive", "-i", help="Approve/reject one by one.")] = False,
    auto_apply: Annotated[bool, typer.Option("--auto-apply", "-a", help="Bulk-approve all pending proposals above min-confidence without interaction.")] = False,
    limit: Annotated[Optional[int], typer.Option("--limit", help="Max proposals to display.")] = None,
    offset: Annotated[Optional[int], typer.Option("--offset", help="Skip first N proposals.")] = None,
):
    """[bold]Review[/bold] pending proposals before applying them."""
    _init_db(db)

    if dupes:
        _review_dupes(limit=limit, offset=offset)
        return

    # Auto-approve mode: bulk-approve and skip interactive preview
    if auto_apply:
        if not session_id:
            console.print("[red]Specify --session before bulk approval.[/red]")
            raise typer.Exit(2)
        count = _bulk_approve(min_confidence=min_confidence, session_id=session_id)
        if count:
            console.print(
                f"\n[bold green]Approved {count} proposals[/bold green] "
                f"(confidence >= {min_confidence})."
            )
            console.print(
                f"Run [bold]ddh execute --session {session_id} --commit[/bold] "
                f"to apply them to disk."
            )
        else:
            console.print(
                f"\n[yellow]No pending proposals meet confidence >= {min_confidence}.[/yellow]"
            )
        return

    from donedatahoarder.executor import preview
    preview(min_confidence=min_confidence, limit=limit, offset=offset,
            session_id=session_id)

    if interactive:
        _interactive_review(limit=limit, offset=offset, session_id=session_id)
    else:
        console.print(
            "\nRun [bold]ddh execute --session <id>[/bold] to preview reviewed proposals, "
            "or add [bold]--commit[/bold] to apply them."
        )


def _review_dupes(
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> None:
    from donedatahoarder.core.dedup import duplicate_summary

    groups = duplicate_summary()
    if not groups:
        console.print("[yellow]No duplicate groups found. Run 'ddh dedup' first.[/yellow]")
        return

    start = offset or 0
    end = (start + limit) if limit else len(groups)
    for g in groups[start:end]:
        table = Table(
            title=f"Group {g['group_id']} ({g['type']}) — "
                  f"{g['count']} files — "
                  f"wasted: {g['wasted_bytes'] / 1024 / 1024:.1f} MB",
            show_lines=True,
        )
        table.add_column("Keep?", width=6)
        table.add_column("Path", overflow="fold")

        for path in g["files"]:
            keep = "★" if g["keep_id"] and path == g.get("keep_path") else ""
            table.add_row(keep, path)

        console.print(table)


def _interactive_review(
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    session_id: str | None = None,
) -> None:
    """One-by-one proposal review loop."""
    from donedatahoarder.db.session import get_engine
    from donedatahoarder.db.models import File, Proposal, ProposalStatus
    from donedatahoarder.core.process_lock import operation_lock
    from sqlalchemy.orm import Session

    engine = get_engine()

    with Session(engine) as session:
        query = (
            session.query(Proposal)
            .filter(Proposal.status == ProposalStatus.PENDING)
            .order_by(Proposal.confidence.desc())
        )
        if session_id:
            query = query.join(File).filter(File.session_id == session_id)
        total = query.count()
        if offset:
            query = query.offset(offset)
        if limit:
            query = query.limit(limit)
        proposals = query.all()

    console.print(f"\n[bold]{len(proposals)} proposals to review[/bold] (total pending: {total}).")
    console.print("Keys: [green]y[/green]=approve  [red]n[/red]=reject  [yellow]s[/yellow]=skip  [bold]q[/bold]=quit\n")

    for i, prop in enumerate(proposals, 1):
        confidence_label = (
            f"conf={prop.confidence:.0%}" if prop.confidence is not None
            else "individual review required"
        )
        console.print(
            f"[dim]{i}/{len(proposals)}[/dim]  "
            f"[cyan]{prop.proposal_type.value}[/cyan]  "
            f"{confidence_label}  "
            f"[red]{Path(prop.current_value or '').name}[/red] -&gt; "
            f"[green]{Path(prop.proposed_value or '').name}[/green]"
        )
        if prop.reasoning:
            console.print(f"  [dim]{prop.reasoning[:100]}[/dim]")

        choice = typer.prompt("  Action", default="s")
        with operation_lock("interactive_review"):
            with Session(engine) as session:
                p = session.get(Proposal, prop.id)
                if (p is None or p.status != ProposalStatus.PENDING
                        or p.current_value != prop.current_value
                        or p.proposed_value != prop.proposed_value):
                    console.print("  [yellow]Proposal changed; review it again.[/yellow]")
                    continue
                if choice.lower() == "y":
                    p.status = ProposalStatus.APPROVED
                    p.review_kind = "individual"
                    console.print("  [green]Approved[/green]")
                elif choice.lower() == "n":
                    p.status = ProposalStatus.REJECTED
                    p.review_kind = "individual"
                    console.print("  [red]Rejected[/red]")
                elif choice.lower() == "q":
                    break
                session.commit()


def _bulk_approve(min_confidence: float = 0.0, session_id: str | None = None) -> int:
    """Bulk-approve safe candidates; near matches need individual review."""
    from donedatahoarder.db.session import get_engine
    from donedatahoarder.db.models import (
        DupeType, DuplicateGroup, File, Proposal, ProposalStatus, ProposalType,
        UserSession,
    )
    from donedatahoarder.core.dependency_protection import ProtectionIndex
    from donedatahoarder.core.process_lock import operation_lock
    from donedatahoarder.executor import _validate_operation_paths
    from sqlalchemy.orm import Session

    engine = get_engine()
    with operation_lock("bulk_review"), Session(engine) as session:
        query = (
            session.query(Proposal)
            .filter(Proposal.status == ProposalStatus.PENDING)
            .filter(Proposal.confidence >= min_confidence)
        )
        if session_id:
            query = query.join(File).filter(File.session_id == session_id)
        user_session = session.get(UserSession, session_id) if session_id else None
        root = Path(user_session.root_path) if user_session and user_session.root_path else None
        protection = ProtectionIndex(root) if root else None
        count = 0
        for proposal in query.all():
            if proposal.proposal_type == ProposalType.MARK_DUPLICATE:
                group = (session.get(DuplicateGroup, proposal.duplicate_group_id)
                         if proposal.duplicate_group_id else None)
                if group is None or group.dupe_type != DupeType.EXACT:
                    continue
            try:
                _validate_operation_paths(proposal, root, session.get(File, proposal.file_id),
                                          protection)
            except (ValueError, OSError):
                continue
            proposal.status = ProposalStatus.APPROVED
            proposal.review_kind = "bulk"
            count += 1
        session.commit()
    return count


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

@app.command()
def execute(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
    commit: Annotated[bool, typer.Option("--commit", help="Apply changes for real (default is dry-run).")] = False,
    session_id: Annotated[Optional[str], typer.Option("--session", "-s", help="Session whose reviewed proposals will be applied.")] = None,
    include_pending: Annotated[bool, typer.Option("--include-pending", help="Also apply pending proposals above --min-confidence.")] = False,
    min_confidence: Annotated[float, typer.Option("--min-confidence", "-c", help="Threshold used only with --include-pending.")] = 0.5,
):
    """
    [bold red]Execute[/bold red] proposals on disk.

    Defaults to dry-run. Pass [bold]--commit[/bold] to make real changes.

    Filesystem changes are journaled for recovery.
    Use [bold]ddh undo --session <id>[/bold] to reverse them.
    """
    _init_db(db)

    from donedatahoarder.executor import execute as do_execute

    if not session_id:
        console.print("[red]Specify --session to select the reviewed work to execute.[/red]")
        raise typer.Exit(2)

    if commit:
        console.print(Panel("[bold red]LIVE RUN — changes will be applied to disk[/bold red]", style="red"))
        confirm = typer.confirm("Are you sure you want to apply changes?", default=False)
        if not confirm:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(0)
    else:
        console.print(Panel("[bold yellow]DRY RUN — no files will be changed[/bold yellow]", style="yellow"))

    do_execute(
        dry_run=not commit,
        min_confidence=min_confidence,
        session_id=session_id,
        include_pending=include_pending,
    )


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------

@app.command()
def undo(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
    last: Annotated[bool, typer.Option("--last", help="Undo outstanding operations in the most recent session with pending recovery work.")] = True,
    session_id: Annotated[Optional[str], typer.Option("--session", "-s", help="Undo operations from a specific session.")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation prompt.")] = False,
    list_sessions: Annotated[bool, typer.Option("--list", "-l", help="List available undo sessions.")] = False,
):
    """[bold red]Undo[/bold red] outstanding journaled operations."""
    from donedatahoarder.core.undo_log import (
        undo_operations, list_undo_sessions, latest_undo_session_id,
        get_last_session_entries,
    )
    _init_db(db)

    if list_sessions:
        sessions = list_undo_sessions()
        if not sessions:
            console.print("[yellow]No undo sessions found.[/yellow]")
            return

        table = Table(title="Undo Sessions", show_lines=True)
        table.add_column("Time", style="cyan")
        table.add_column("Operations", style="bold")
        table.add_column("Duration", style="dim")

        for s in reversed(sessions[-10:]):  # Show last 10
            ops = ", ".join(f"{k}: {v}" for k, v in s["operations"].items())
            duration = f"{s['duration_seconds']:.1f}s"
            ts = s["timestamp"][:19].replace("T", " ")  # Format ISO timestamp
            table.add_row(ts, f"{s['operation_count']} ops ({ops})", duration)

        console.print(table)
        return

    if not last and not session_id:
        console.print("[yellow]Use --last to undo the most recent operations, or --session <id> for a specific session.[/yellow]")
        console.print("Use --list to see available sessions.")
        raise typer.Exit(1)

    if not session_id and last:
        session_id = latest_undo_session_id()
    if not session_id and not get_last_session_entries():
        console.print("[yellow]No outstanding session operations to undo.[/yellow]")
        return

    undo_operations(
        session_id=session_id,
        force=force,
        console=console,
    )


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

@app.command()
def stats(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
):
    """Show database statistics and progress summary."""
    _init_db(db)

    from donedatahoarder.db.session import get_engine
    from donedatahoarder.db.models import File, Proposal, ProposalStatus, DuplicateGroup
    from sqlalchemy.orm import Session
    from sqlalchemy import func

    engine = get_engine()
    with Session(engine) as session:
        total_files = session.query(func.count(File.id)).scalar()
        status_counts = (
            session.query(File.status, func.count(File.id))
            .group_by(File.status)
            .all()
        )
        total_proposals = session.query(func.count(Proposal.id)).scalar()
        pending_proposals = (
            session.query(func.count(Proposal.id))
            .filter(Proposal.status == ProposalStatus.PENDING)
            .scalar()
        )
        dupe_groups = session.query(func.count(DuplicateGroup.id)).scalar()
        total_size = session.query(func.sum(File.size_bytes)).scalar() or 0

    table = Table(title="DoneDataHoarder Stats", show_lines=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="bold")

    table.add_row("Total files indexed", f"{total_files:,}")
    table.add_row("Total size", f"{total_size / 1024**3:.2f} GB")
    table.add_row("", "")
    for status, count in sorted(status_counts, key=lambda x: x[1], reverse=True):
        table.add_row(f"  Status: {status.value}", f"{count:,}")
    table.add_row("", "")
    table.add_row("Pending proposals", f"{pending_proposals:,}")
    table.add_row("Total proposals", f"{total_proposals:,}")
    table.add_row("Duplicate groups", f"{dupe_groups:,}")

    console.print(table)


# ---------------------------------------------------------------------------
# pipeline (run all steps in sequence)
# ---------------------------------------------------------------------------

@app.command()
def preflight(
    root: Annotated[Path, typer.Argument(help="Collection directory to size without reading file contents.")],
    mode: Annotated[str, typer.Option("--mode", help="full|representative|metadata_only")] = "full",
    sequence_sample_stride: Annotated[int, typer.Option("--sequence-sample-stride", help="For representative mode, analyze every Nth confirmed numbered visual frame.")] = 10,
    model_seconds_per_file: Annotated[float, typer.Option("--model-seconds-per-file", help="Measured baseline seconds per AI call; default 5.")] = 5.0,
):
    """Estimate collection time and disk needs from file metadata only."""
    import json
    from donedatahoarder.core.preflight import estimate_collection

    try:
        result = estimate_collection(
            root, mode=mode, sequence_sample_stride=sequence_sample_stride,
            model_seconds_per_file=model_seconds_per_file,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    console.print(json.dumps(result, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# pipeline (run all steps in sequence)
# ---------------------------------------------------------------------------

@app.command()
def pipeline(
    root: Annotated[Path, typer.Argument(help="Directory to process end-to-end.")],
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
    backend: Annotated[str, typer.Option("--backend", help="AI backend: ollama|gemini|auto", envvar="DDH_BACKEND")] = "ollama",
    ollama_host: Annotated[str, typer.Option("--ollama-host", help="Ollama server URL.", envvar="OLLAMA_HOST")] = "http://localhost:11434",
    model: Annotated[str, typer.Option("--model", help="Model name.", envvar="DDH_MODEL")] = "gemma3:12b",
    workers: Annotated[int, typer.Option("--workers", "-w")] = 1,
    skip_analyze: Annotated[bool, typer.Option("--skip-analyze")] = False,
    sequence_sample_stride: Annotated[int, typer.Option("--sequence-sample-stride", help="Analyze every Nth numbered image frame; 0 analyzes all.")] = 0,
    use_cache: Annotated[bool, typer.Option("--cache/--no-cache")] = True,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = True,
):
    """
    [bold]Run the full pipeline[/bold]: scan -&gt; enrich -&gt; dedup -&gt; analyze -&gt; propose -&gt; (preview).

    Does NOT execute changes unless you follow up with [bold]ddh execute --commit[/bold].
    """
    _init_db(db)
    root = root.resolve()
    if not root.is_dir():
        console.print(f"[red]Directory does not exist: {root}[/red]")
        raise typer.Exit(1)

    # A pipeline run owns one collection.  Passing its ID to every stage keeps
    # files and proposals in other collections out of this run's review.
    from donedatahoarder.db.models import SessionStatus, UserSession
    from donedatahoarder.db.session import get_engine
    from sqlalchemy.orm import Session

    with Session(get_engine()) as session:
        collection = UserSession(
            name=root.name,
            root_path=str(root),
            backend=backend,
            model=model,
            analyze_model=model,
            propose_model=model,
            workers=workers,
            status=SessionStatus.ACTIVE,
        )
        session.add(collection)
        session.commit()
        session_id = collection.id

    console.print(Panel(f"[bold]Full pipeline on:[/bold] {root}", style="bold cyan"))
    console.print(f"Collection session: [cyan]{session_id}[/cyan]")

    # scan
    from donedatahoarder.core.scanner import scan as do_scan
    console.print("\n[bold cyan]Step 1/5: Scanning…[/bold cyan]")
    do_scan(root, workers=workers, session_id=session_id)

    # enrich
    from donedatahoarder.core.enricher import enrich as do_enrich
    console.print("\n[bold green]Step 2/5: Enriching…[/bold green]")
    do_enrich(workers=workers, session_id=session_id)

    # dedup
    from donedatahoarder.core.dedup import (
        find_exact_duplicates, find_perceptual_duplicates,
        find_text_near_duplicates, find_semantic_duplicates,
        generate_dedup_proposals,
    )
    console.print("\n[bold yellow]Step 3/5: Deduplicating…[/bold yellow]")
    exact = find_exact_duplicates(session_id=session_id)
    perceptual = find_perceptual_duplicates(session_id=session_id)
    text_matches = find_text_near_duplicates(session_id=session_id)
    console.print(f"Exact duplicate groups: {exact['groups']}; "
                  f"similar-image candidates: {perceptual.get('groups', 0)}; "
                  f"similar-text candidates: {text_matches['groups']}")
    _print_duplicate_coverage("Perceptual", perceptual)
    _print_duplicate_coverage("Text", text_matches)

    # analyze
    if not skip_analyze:
        console.print("\n[bold magenta]Step 4/5: Analyzing with AI…[/bold magenta]")
        try:
            _init_ai(backend, ollama_host, model)
            from donedatahoarder.analyzers.pipeline import analyze as do_analyze
            analysis = do_analyze(workers=workers, session_id=session_id,
                                  sequence_sample_stride=sequence_sample_stride,
                                  use_cache=use_cache)
            console.print(
                f"Analysis: {analysis.get('analyzed', 0)} fresh, "
                f"{analysis.get('cached', 0)} cached, "
                f"{analysis.get('sampled', 0)} sampled, "
                f"{analysis.get('skipped', 0)} skipped, "
                f"{analysis.get('errors', 0)} errors"
            )
        except RuntimeError as exc:
            console.print(f"[yellow]AI analysis skipped: {exc}[/yellow]")
    else:
        console.print("\n[dim]Step 4/5: AI analysis skipped (--skip-analyze)[/dim]")

    semantic = find_semantic_duplicates(session_id=session_id)
    console.print(f"Semantic similarity candidates: {semantic['groups']} groups")
    _print_duplicate_coverage("Semantic", semantic)
    proposals = generate_dedup_proposals(session_id=session_id)
    console.print(f"Duplicate review proposals created: {proposals['created']}")

    # propose
    console.print("\n[bold blue]Step 5/5: Generating proposals…[/bold blue]")
    from donedatahoarder.proposals.namer import generate_proposals
    generate_proposals(session_id=session_id)

    if dry_run:
        console.print("\n[bold yellow]Previewing reviewed proposals…[/bold yellow]")
        from donedatahoarder.executor import execute as do_execute
        do_execute(dry_run=True, session_id=session_id)

    # summary
    console.print("\n")
    console.print(f"Review session [cyan]{session_id}[/cyan] before executing changes.")

    console.print(
        Panel(
            "[bold green]Pipeline complete![/bold green]\n\n"
            "Next steps:\n"
            f"  • [cyan]ddh review --session {session_id}[/cyan] — inspect proposals\n"
            f"  • [cyan]ddh execute --session {session_id}[/cyan] — preview approvals\n"
            f"  • [cyan]ddh execute --session {session_id} --commit[/cyan] — apply approvals\n",
            style="green",
        )
    )


# ---------------------------------------------------------------------------
# serve (web UI)
# ---------------------------------------------------------------------------

@app.command()
def serve(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
    host: Annotated[str, typer.Option("--host", help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port number.")] = 8080,
):
    """[bold]Launch[/bold] the web review UI in your browser."""
    import uvicorn
    from donedatahoarder.web.app import create_app

    db_path = Path(db)
    web_app = create_app(db_path)

    console.print(
        Panel(
            f"[bold green]DoneDataHoarder Web UI[/bold green]\n\n"
            f"  Open [cyan]http://{host}:{port}[/cyan] in your browser\n"
            f"  Database: [dim]{db_path.resolve()}[/dim]\n"
            f"  Press Ctrl+C to stop",
            style="green",
        )
    )

    uvicorn.run(web_app, host=host, port=port, log_level="warning")


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

@app.command()
def models(
    ollama_host: Annotated[str, typer.Option("--ollama-host", help="Ollama server URL.", envvar="OLLAMA_HOST")] = "http://localhost:11434",
    recommend: Annotated[bool, typer.Option("--recommend", help="Recommend a model based on available RAM.")] = False,
):
    """[bold]List[/bold] installed Ollama models and check vision support."""
    from donedatahoarder.ai.ollama_client import OllamaClient

    client = OllamaClient(host=ollama_host)
    if not client.is_available():
        console.print(f"[red]Ollama not reachable at {ollama_host}.[/red]")
        console.print("Start Ollama with: [bold]ollama serve[/bold]")
        raise typer.Exit(1)

    installed = client.list_models()
    if not installed:
        console.print("[yellow]No models installed.[/yellow]")
        console.print("Install a vision model: [bold]ollama pull gemma3:12b[/bold]")
        return

    table = Table(title="Installed Ollama Models", show_lines=True)
    table.add_column("Model", style="cyan")
    table.add_column("Vision", style="bold")

    vision_models = {"llava", "bakllava", "gemma3", "moondream", "cogvlm"}
    for name in installed:
        has_vision = any(vm in name.lower() for vm in vision_models)
        table.add_row(
            name,
            "[green]Yes[/green]" if has_vision else "[dim]No[/dim]",
        )
    console.print(table)

    if recommend:
        try:
            import psutil
            ram_gb = psutil.virtual_memory().total / 1024 ** 3
            console.print(f"\nDetected RAM: [bold]{ram_gb:.1f} GB[/bold]")
            if ram_gb >= 16:
                console.print("Recommendation: [green]gemma3:12b[/green] (best quality, vision)")
            elif ram_gb >= 8:
                console.print("Recommendation: [green]gemma3:4b[/green] (good balance)")
            elif ram_gb >= 4:
                console.print("Recommendation: [yellow]gemma3:1b[/yellow] (fast, lower quality)")
            else:
                console.print("Recommendation: [red]Insufficient RAM[/red] for local LLMs; use Gemini backend.")
        except ImportError:
            console.print("\n[yellow]Install psutil for RAM-based recommendations:[/yellow] pip install psutil")
            console.print("Default recommendation: [green]gemma3:12b[/green] (vision-capable)")


# ---------------------------------------------------------------------------
# bench
# ---------------------------------------------------------------------------

@app.command()
def bench(
    tmp_dir: Annotated[Optional[Path], typer.Option("--tmp-dir", help="Directory to create synthetic files in.")] = None,
    file_count: Annotated[int, typer.Option("--files", help="Number of synthetic files to create.")] = 100,
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DDH_DB")] = "donedatahoarder.db",
):
    """[bold]Benchmark[/bold] scan speed on a synthetic corpus."""
    import tempfile
    import time

    from donedatahoarder.core.scanner import scan as do_scan

    _init_db(db)

    test_dir = tmp_dir or Path(tempfile.mkdtemp(prefix="datahoarder_bench_"))
    test_dir.mkdir(parents=True, exist_ok=True)

    console.print(Panel(f"Creating {file_count} synthetic files in {test_dir}…", style="cyan"))

    for i in range(file_count):
        (test_dir / f"file_{i:04d}.txt").write_text(f"benchmark content {i}\n" * 50)
        if i % 10 == 0:
            (test_dir / f"image_{i:04d}.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 1024)

    console.print("[green]Created.[/green] Running scan…")
    start = time.perf_counter()
    counts = do_scan(test_dir, workers=1)
    elapsed = time.perf_counter() - start

    files_per_sec = counts["new"] / elapsed if elapsed > 0 else 0
    console.print(
        f"\n[bold green]Scan benchmark complete[/bold green] — "
        f"{counts['new']} files in {elapsed:.2f}s "
        f"([cyan]{files_per_sec:.1f}[/cyan] files/sec)"
    )

    # Cleanup
    import shutil
    shutil.rmtree(test_dir, ignore_errors=True)
    console.print(f"[dim]Cleaned up {test_dir}[/dim]")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@app.command()
def config(
    edit_naming: Annotated[bool, typer.Option("--edit-naming", help="Open naming_rules.json in your default editor.")] = False,
    reset_naming: Annotated[bool, typer.Option("--reset-naming", help="Reset naming_rules.json to built-in defaults.")] = False,
    edit_phash: Annotated[bool, typer.Option("--edit-phash", help="Open phash_config.json in your default editor.")] = False,
    reset_phash: Annotated[bool, typer.Option("--reset-phash", help="Reset phash_config.json to built-in defaults.")] = False,
):
    """[bold]Manage[/bold] DoneDataHoarder configuration files."""
    from donedatahoarder.config import (
        _DEFAULT_NAMING_RULES_FILE,
        load_naming_rules,
        save_naming_rules,
        _DEFAULT_USELESS_STEM_PATTERNS,
        _DEFAULT_HYGIENE_CONFIG,
        _DEFAULT_PHASH_CONFIG_FILE,
        load_phash_config,
        save_phash_config,
    )

    if reset_naming:
        defaults = {
            "useless_stem_patterns": _DEFAULT_USELESS_STEM_PATTERNS,
            "hygiene": _DEFAULT_HYGIENE_CONFIG,
            "user_patterns": [],
        }
        save_naming_rules(defaults)
        console.print(f"[green]Reset[/green] naming rules to defaults: {_DEFAULT_NAMING_RULES_FILE}")
        return

    if edit_naming:
        path = _DEFAULT_NAMING_RULES_FILE
        if not path.exists():
            defaults = {
                "useless_stem_patterns": _DEFAULT_USELESS_STEM_PATTERNS,
                "hygiene": _DEFAULT_HYGIENE_CONFIG,
                "user_patterns": [],
            }
            save_naming_rules(defaults)
            console.print(f"[green]Created[/green] default naming rules: {path}")
        import subprocess
        import platform
        if platform.system() == "Windows":
            subprocess.run(["notepad", str(path)])
        elif platform.system() == "Darwin":
            subprocess.run(["open", "-t", str(path)])
        else:
            editor = os.environ.get("EDITOR", "nano")
            subprocess.run([editor, str(path)])
        console.print(f"[green]Saved[/green] naming rules: {path}")
        return

    if reset_phash:
        from donedatahoarder.phash import DEFAULT_ALGORITHM, DEFAULT_HASH_SIZE, DEFAULT_THRESHOLD, DEFAULT_VIDEO_ENABLED
        defaults = {
            "algorithm": DEFAULT_ALGORITHM,
            "hash_size": DEFAULT_HASH_SIZE,
            "threshold": DEFAULT_THRESHOLD,
            "video_enabled": DEFAULT_VIDEO_ENABLED,
        }
        save_phash_config(defaults)
        console.print(f"[green]Reset[/green] perceptual hash config to defaults: {_DEFAULT_PHASH_CONFIG_FILE}")
        return

    if edit_phash:
        path = _DEFAULT_PHASH_CONFIG_FILE
        if not path.exists():
            from donedatahoarder.phash import DEFAULT_ALGORITHM, DEFAULT_HASH_SIZE, DEFAULT_THRESHOLD, DEFAULT_VIDEO_ENABLED
            defaults = {
                "algorithm": DEFAULT_ALGORITHM,
                "hash_size": DEFAULT_HASH_SIZE,
                "threshold": DEFAULT_THRESHOLD,
                "video_enabled": DEFAULT_VIDEO_ENABLED,
            }
            save_phash_config(defaults)
            console.print(f"[green]Created[/green] default perceptual hash config: {path}")
        import subprocess
        import platform
        if platform.system() == "Windows":
            subprocess.run(["notepad", str(path)])
        elif platform.system() == "Darwin":
            subprocess.run(["open", "-t", str(path)])
        else:
            editor = os.environ.get("EDITOR", "nano")
            subprocess.run([editor, str(path)])
        console.print(f"[green]Saved[/green] perceptual hash config: {path}")
        return

    # Default: show current config status
    rules = load_naming_rules()
    table = Table(title="Naming Rules", show_lines=True)
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="bold")
    table.add_row(
        "Built-in patterns",
        str(len(rules.get("useless_stem_patterns", []))),
    )
    table.add_row(
        "User patterns",
        str(len(rules.get("user_patterns", []))),
    )
    table.add_row(
        "Config file",
        str(_DEFAULT_NAMING_RULES_FILE),
    )
    console.print(table)

    phash = load_phash_config()
    ptable = Table(title="Perceptual Hash Config", show_lines=True)
    ptable.add_column("Setting", style="cyan")
    ptable.add_column("Value", style="bold")
    ptable.add_row("Algorithm", phash.get("algorithm", "phash"))
    ptable.add_row("Hash size", str(phash.get("hash_size", 8)))
    ptable.add_row("Threshold", str(phash.get("threshold", 8)))
    ptable.add_row("Video enabled", "Yes" if phash.get("video_enabled", True) else "No")
    ptable.add_row("Config file", str(_DEFAULT_PHASH_CONFIG_FILE))
    console.print(ptable)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@app.command()
def tui(
    root: Annotated[Optional[str], typer.Argument(help="Folder to organize (on the workstation when using --connect); omit to choose a folder or session.")] = None,
    session_id: Annotated[Optional[str], typer.Option("--session", help="Resume an existing session instead of opening a folder.")] = None,
    db: Annotated[Optional[Path], typer.Option("--db", help="SQLite database; defaults to XDG_DATA_HOME/donedatahoarder/index.db.", envvar="DDH_DB")] = None,
    model: Annotated[str, typer.Option("--model", help="Local Ollama model.", envvar="DDH_MODEL")] = "gemma3:12b",
    ollama_host: Annotated[str, typer.Option("--ollama-host", help="Ollama server URL.", envvar="OLLAMA_HOST")] = "http://localhost:11434",
    workers: Annotated[int, typer.Option("--workers", "-w", min=1, max=32, help="Analysis worker count.")] = 1,
    images: Annotated[str, typer.Option("--images", help="Image renderer: auto, sixel, kitty, or off.")] = "auto",
    connect: Annotated[Optional[str], typer.Option("--connect", help="Remote workstation URL: verified HTTPS or loopback HTTP through SSH.")] = None,
    token_file: Annotated[Optional[Path], typer.Option("--token-file", help="Private remote connection token file.")] = None,
    ca_file: Annotated[Optional[Path], typer.Option("--ca-file", help="Trusted workstation TLS certificate/CA PEM file.")] = None,
    discover: Annotated[bool, typer.Option("--discover", help="Find nearby workstations and pair or open a saved device.")] = False,
):
    """Open the Omarchy-oriented terminal pipeline and review workspace (Python 3.12+)."""
    if images not in {"auto", "sixel", "kitty", "off"}:
        raise typer.BadParameter("Choose auto, sixel, kitty, or off.", param_hint="--images")
    if root is not None and session_id:
        raise typer.BadParameter("Choose a folder or --session, not both.")
    if discover and (connect or token_file or ca_file):
        raise typer.BadParameter("Choose --discover or the manual --connect options.")
    if not connect and (token_file or ca_file):
        raise typer.BadParameter("Use --connect with --token-file or --ca-file.")
    if connect and not token_file:
        raise typer.BadParameter("Remote connections require --token-file.")
    if (connect or discover) and db is not None:
        raise typer.BadParameter("Remote sessions use the workstation database; omit --db (including DDH_DB).")
    from donedatahoarder.tui.launch import launch
    try:
        remote_options = {"connect": connect, "token_file": token_file, "ca_file": ca_file} if connect else {}
        if discover:
            remote_options["discover"] = True
        folder = root if connect or discover else Path(root) if root is not None else None
        launch(folder, session_id=session_id, db_path=db, model=model,
               ollama_host=ollama_host, workers=workers, images=images, **remote_options)
    except (RuntimeError, ValueError, OSError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc


@app.command("remote-serve")
def remote_serve(
    roots: Annotated[list[Path], typer.Option("--root", help="Allowed workstation collection folder; repeat for multiple roots.")],
    token_file: Annotated[Path, typer.Option("--token-file", help="Private token file; generated if absent. Share it with your laptop securely.")],
    db: Annotated[Optional[Path], typer.Option("--db", help="Workstation SQLite index; defaults to the TUI index.", envvar="DDH_DB")] = None,
    host: Annotated[str, typer.Option("--host", help="Listener; non-loopback requires TLS.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
    cert_file: Annotated[Optional[Path], typer.Option("--cert-file", help="TLS certificate PEM for direct LAN connections.")] = None,
    key_file: Annotated[Optional[Path], typer.Option("--key-file", help="TLS private key PEM.")] = None,
    name: Annotated[Optional[str], typer.Option("--name", help="Workstation name displayed in connected TUIs.")] = None,
    model: Annotated[str, typer.Option("--model", envvar="DDH_MODEL")] = "gemma3:12b",
    workers: Annotated[int, typer.Option("--workers", min=1, max=32)] = 1,
    ollama_host: Annotated[str, typer.Option("--ollama-host", envvar="OLLAMA_HOST")] = "http://localhost:11434",
    discoverable: Annotated[bool, typer.Option("--discoverable", help="Advertise this workstation on the LAN using managed HTTPS.")] = False,
    pair: Annotated[bool, typer.Option("--pair", help="With --discoverable, print a ten-minute, one-use pairing invitation.")] = False,
):
    """Run the authenticated workstation service for remote terminal sessions."""
    from donedatahoarder.remote.config import ensure_token, validate_listener, validate_control_paths
    from donedatahoarder.core.undo_log import get_datahoarder_dir
    from donedatahoarder.tui.launch import default_database
    try:
        if pair and not discoverable:
            raise ValueError("Use --pair with --discoverable.")
        if discoverable:
            from donedatahoarder.remote.config import is_loopback
            if is_loopback(host):
                raise ValueError("Nearby workstations need a LAN listener; use --host 0.0.0.0 or a LAN IP.")
            if cert_file or key_file:
                raise ValueError("Discoverable mode manages its own TLS identity; omit --cert-file and --key-file.")
            try:
                import zeroconf  # noqa: F401
                import cryptography  # noqa: F401
            except ImportError as exc:
                raise RuntimeError("Install nearby support: python -m pip install 'donedatahoarder[remote,nearby]'") from exc
        else:
            validate_listener(host, cert_file, key_file)
        if not roots or any(not root.expanduser().is_absolute() for root in roots):
            raise ValueError("Each --root must be an absolute workstation folder path.")
        try:
            import uvicorn
            from donedatahoarder.remote.server import create_app
        except ImportError as exc:
            raise RuntimeError("Install the workstation service: python -m pip install 'donedatahoarder[remote]'") from exc
        database = (db or default_database()).expanduser().resolve()
        control_paths = {"database": database, "connection token": token_file,
                         "recovery journal": get_datahoarder_dir(create=False)}
        pairing_path = Path(str(database) + ".remote-devices.sqlite3")
        tls_directory = Path(str(database) + ".remote-tls")
        control_paths.update({"device credentials": pairing_path, "TLS identity": tls_directory})
        if key_file:
            control_paths["TLS private key"] = key_file
        validate_control_paths(control_paths, roots)
        token = ensure_token(token_file)
        database.parent.mkdir(parents=True, exist_ok=True)
        from donedatahoarder.core.process_lock import operation_lock
        # Separate from the collection writer lease: hold this for the entire
        # daemon so a second server cannot reconcile still-running receipts.
        with operation_lock("remote session server", db_path=Path(str(database) + ".remote-daemon")):
            pairing_options = {"pairing_path": pairing_path} if discoverable or pairing_path.exists() else {}
            remote_app = create_app(database, token=token, allowed_roots=roots,
                                    model=model, workers=workers, ollama_host=ollama_host, name=name, **pairing_options)
            if discoverable:
                from donedatahoarder.remote.pairing import ensure_tls
                cert_file, key_file, tls_hostname = ensure_tls(tls_directory, remote_app.state.remote_receipts.server_id)
                if pair:
                    invitation = remote_app.state.remote_pairing.create_invitation(cert_file.read_text(encoding="ascii"), tls_hostname)
                    console.print("Pairing is open for ten minutes. Paste this private, one-use invitation into the laptop's Nearby workstations dialog:", markup=False)
                    console.print(invitation, markup=False, soft_wrap=True)
            scheme = "https" if cert_file else "http"
            console.print(f"Remote sessions: {scheme}://{host}:{port}\nDatabase: {database}\nPrivate token file: {token_file.resolve()}\nProcessing continues when a client disconnects. Keep this process running.", markup=False)
            if discoverable:
                import socket
                from donedatahoarder.remote.runtime import run_discoverable
                run_discoverable(remote_app, host=host, port=port, cert_file=cert_file,
                                 key_file=key_file, hostname=tls_hostname, name=name or socket.gethostname())
            else:
                uvicorn.run(remote_app, host=host, port=port, workers=1, log_level="warning",
                            proxy_headers=False,
                            ssl_certfile=str(cert_file) if cert_file else None,
                            ssl_keyfile=str(key_file) if key_file else None)
    except (RuntimeError, ValueError, OSError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc


def _workstation_pairing_store(database: Path):
    from donedatahoarder.remote.pairing import PairingStore
    import sqlite3
    path = Path(str(database) + ".remote-devices.sqlite3")
    receipts_path = Path(str(database) + ".remote-receipts.sqlite3")
    if not path.is_file() or not receipts_path.is_file():
        raise ValueError("No paired-device store for this workstation index. Start remote-serve --discoverable first.")
    with sqlite3.connect(receipts_path.as_uri() + "?mode=ro", uri=True) as receipts:
        row = receipts.execute("SELECT value FROM remote_metadata WHERE key='server_id'").fetchone()
        if row is None:
            raise ValueError("Workstation identity is missing; preserve the index and receipt database.")
    return PairingStore(path, row[0])


@app.command("remote-pair")
def remote_pair(
    db: Annotated[Optional[Path], typer.Option("--db", help="Running workstation's SQLite index.", envvar="DDH_DB")] = None,
):
    """Issue a fresh ten-minute invitation without stopping workstation jobs."""
    from donedatahoarder.tui.launch import default_database
    from donedatahoarder.remote.pairing import ensure_tls
    import sqlite3
    try:
        database = (db or default_database()).expanduser().resolve()
        store = _workstation_pairing_store(database)
        tls_directory = Path(str(database) + ".remote-tls")
        if not (tls_directory / "certificate.pem").is_file():
            raise ValueError("Start remote-serve --discoverable to create this workstation's TLS identity.")
        certificate, _, hostname = ensure_tls(tls_directory, store.server_id)
        invitation = store.create_invitation(certificate.read_text(encoding="ascii"), hostname)
        console.print("Paste this private, one-use invitation into the laptop. It expires in ten minutes and replaces any earlier unused invitation:", markup=False)
        console.print(invitation, markup=False, soft_wrap=True)
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc


@app.command("remote-devices")
def remote_devices(
    db: Annotated[Optional[Path], typer.Option("--db", help="Workstation SQLite index.", envvar="DDH_DB")] = None,
    revoke: Annotated[Optional[str], typer.Option("--revoke", help="Revoke a paired device by its full ID.")] = None,
):
    """List or revoke paired laptops on the workstation; no network service needed."""
    from donedatahoarder.tui.launch import default_database
    import sqlite3
    try:
        database = (db or default_database()).expanduser().resolve()
        store = _workstation_pairing_store(database)
        if revoke:
            if not store.revoke(revoke):
                raise ValueError("No paired device has that ID.")
            console.print("Device credential revoked.", markup=False)
        for device in store.list_devices():
            console.print(f"{device['device_id']}  {device['name']}  {'revoked' if device.get('revoked_at') is not None else 'active'}", markup=False)
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc


@app.command("tui-fixture")
def tui_fixture(
    destination: Annotated[Path, typer.Argument(help="New directory for synthetic images, isolated index, and manifest; must not exist.")],
    index: Annotated[bool, typer.Option("--index/--no-index", help="Run the real metadata-only pipeline; no AI calls or applied changes.")] = True,
):
    """Create a disposable native-image qualification kit, ready to open in the TUI."""
    from donedatahoarder.tui.qualification import create_fixture
    try:
        result = create_fixture(destination, index=index)
    except (RuntimeError, ValueError, OSError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc
    console.print(f"Created {len(result['files'])} synthetic diagnostic images in {result['root']}", markup=False)
    console.print(f"Manifest and launch command: {Path(result['root']).parent / 'START.txt'}", markup=False)
    if result["session_id"]:
        console.print(f"Metadata session: {result['session_id']}", markup=False)
    console.print("Native image qualification remains pending; follow docs/tui-qualification.md.", markup=False)


@app.command("tui-diagnostics")
def tui_diagnostics(
    images: Annotated[str, typer.Option("--images", help="Probe auto, sixel, kitty, or off before opening the app.")] = "auto",
    output: Annotated[Optional[Path], typer.Option("--output", help="Create a JSON report at a new path; defaults to stdout.")] = None,
    terminal_name: Annotated[Optional[str], typer.Option("--terminal-name", help="Your reported terminal name; not inferred from TERM.")] = None,
    terminal_version: Annotated[Optional[str], typer.Option("--terminal-version", help="Your reported terminal version.")] = None,
    omarchy_version: Annotated[Optional[str], typer.Option("--omarchy-version", help="Your reported Omarchy version.")] = None,
):
    """Collect local terminal capabilities and a pending native qualification checklist."""
    if images not in {"auto", "sixel", "kitty", "off"}:
        raise typer.BadParameter("Choose auto, sixel, kitty, or off.", param_hint="--images")
    import json
    from donedatahoarder.tui.diagnostics import collect_diagnostics, write_report
    try:
        report = collect_diagnostics(images=images, terminal_name=terminal_name,
                                     terminal_version=terminal_version, omarchy_version=omarchy_version)
        if output is None:
            typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            path = write_report(report, output)
            console.print(f"Saved {path}. Native checks remain not_run.", markup=False)
    except (RuntimeError, ValueError, OSError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()

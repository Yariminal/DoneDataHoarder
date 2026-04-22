"""
DataHoarder CLI — entry point for all commands.

Usage:
    datahoarder scan     /path/to/drive
    datahoarder enrich
    datahoarder analyze  [--workers N] [--limit N]
    datahoarder dedup
    datahoarder propose
    datahoarder review
    datahoarder execute  [--commit]
    datahoarder stats
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
    name="datahoarder",
    help="AI-powered file organization for data hoarders.",
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()


@app.callback()
def main_callback(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging to console.", envvar="DATAHOARDER_LOG_VERBOSE")] = False,
):
    """Global options for all DataHoarder commands."""
    from datahoarder.logging import setup_logging
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
            "[bold green]Welcome to DataHoarder![/bold green]\n\n"
            "Your AI-powered file organization assistant.\n"
            "  • Run [cyan]datahoarder doctor[/cyan] to check your setup\n"
            "  • Run [cyan]datahoarder scan /path/to/files[/cyan] to get started\n"
            "  • Docs: [blue]https://github.com/Yariminal/DoneDataHoarder[/blue]",
            title="🗄️  DataHoarder",
            style="green",
        )
    )
    try:
        welcome_file.parent.mkdir(parents=True, exist_ok=True)
        welcome_file.touch()
    except OSError:
        pass


def _init_db(db_path: str) -> Path:
    from datahoarder.db.session import init_db
    p = Path(db_path)
    init_db(p)
    return p


def _init_ai(backend: str, ollama_host: str, model: str) -> None:
    from datahoarder.ai.router import init_ai
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
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
    ollama_host: Annotated[str, typer.Option("--ollama-host", help="Ollama server URL.", envvar="OLLAMA_HOST")] = "http://localhost:11434",
    model: Annotated[str, typer.Option("--model", help="Ollama model name to check.", envvar="DATAHOARDER_MODEL")] = "gemma3:12b",
    backend: Annotated[str, typer.Option("--backend", help="AI backend: ollama|gemini|auto", envvar="DATAHOARDER_BACKEND")] = "ollama",
):
    """[bold green]Diagnose[/bold green] the environment: Ollama, disk space, and DB integrity."""
    import shutil
    import sqlite3

    from datahoarder.ai.ollama_client import OllamaClient

    console.print(Panel("Running diagnostics…", style="green"))
    table = Table(title="Doctor Report", show_lines=True)
    table.add_column("Check", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Detail", style="dim")

    # --- Ollama reachability ---
    ollama = OllamaClient(host=ollama_host, text_model=model, vision_model=model)
    if ollama.is_available():
        models = ollama.list_models()
        if model in models:
            table.add_row("Ollama reachability", "[green]OK[/green]", f"Reachable at {ollama_host}")
            table.add_row("Ollama model", "[green]OK[/green]", f"{model} is installed")
        else:
            table.add_row("Ollama reachability", "[green]OK[/green]", f"Reachable at {ollama_host}")
            table.add_row(
                "Ollama model",
                "[yellow]MISSING[/yellow]",
                f"Model '{model}' not found. Install with: [bold]ollama pull {model}[/bold]",
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
            from datahoarder.ai.gemini_client import GeminiClient
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
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
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

    from datahoarder.core.scanner import scan as do_scan
    counts = do_scan(root, force_rescan=force, workers=workers)

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
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
    limit: Annotated[Optional[int], typer.Option("--limit", help="Max files to process.")] = None,
):
    """[bold green]Enrich[/bold green] scanned files with metadata, hashes, and dates."""
    _init_db(db)
    console.print(Panel("Extracting metadata & hashes", style="green"))

    from datahoarder.core.enricher import enrich as do_enrich
    counts = do_enrich(limit=limit)

    console.print(
        f"\n[bold green]Enrichment complete[/bold green] — "
        f"{counts['enriched']} enriched, "
        f"{counts['errors']} errors"
    )


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

@app.command()
def analyze(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
    backend: Annotated[str, typer.Option("--backend", help="AI backend: ollama|gemini|auto", envvar="DATAHOARDER_BACKEND")] = "ollama",
    ollama_host: Annotated[str, typer.Option("--ollama-host", help="Ollama server URL.", envvar="OLLAMA_HOST")] = "http://localhost:11434",
    model: Annotated[str, typer.Option("--model", help="Model name (must support vision).", envvar="DATAHOARDER_MODEL")] = "gemma3:12b",
    workers: Annotated[int, typer.Option("--workers", "-w", help="Parallel workers.")] = 1,
    limit: Annotated[Optional[int], typer.Option("--limit", help="Max files to analyze.")] = None,
    min_size: Annotated[int, typer.Option("--min-size", help="Skip files smaller than N KB.")] = 1,
):
    """[bold magenta]Analyze[/bold magenta] enriched files with AI (vision + text)."""
    _init_db(db)
    console.print(Panel(f"AI analysis — backend: [bold]{backend}[/bold], model: [bold]{model}[/bold]", style="magenta"))

    try:
        _init_ai(backend, ollama_host, model)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    from datahoarder.analyzers.pipeline import analyze as do_analyze
    counts = do_analyze(workers=workers, limit=limit, min_size_kb=min_size)

    console.print(
        f"\n[bold green]Analysis complete[/bold green] — "
        f"{counts['analyzed']} analyzed, "
        f"{counts['skipped']} skipped, "
        f"{counts['errors']} errors"
    )


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------

@app.command()
def dedup(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
    no_perceptual: Annotated[bool, typer.Option("--no-perceptual", help="Skip perceptual image hashing.")] = False,
):
    """[bold yellow]Deduplicate[/bold yellow] — find exact and near-duplicate files."""
    _init_db(db)
    console.print(Panel("Duplicate detection", style="yellow"))

    from datahoarder.core.dedup import find_exact_duplicates, find_perceptual_duplicates, duplicate_summary

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
                f"Near-duplicate images: [bold]{perc['groups']}[/bold] groups, "
                f"[yellow]{perc['duplicates']}[/yellow] redundant files"
            )

    summary = duplicate_summary()
    if summary:
        total_wasted = sum(g["wasted_bytes"] for g in summary)
        mb = total_wasted / 1024 / 1024
        console.print(f"\n[bold]Estimated reclaimable space: [green]{mb:.1f} MB[/green][/bold]")
        console.print("Run [bold]datahoarder review --dupes[/bold] to inspect groups.")


# ---------------------------------------------------------------------------
# relate
# ---------------------------------------------------------------------------

@app.command()
def relate(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
    session_id: Annotated[Optional[str], typer.Option("--session", help="Session ID to process. Defaults to latest.")] = None,
    scope: Annotated[str, typer.Option("--scope", help="'per_directory' (default) or 'cross_directory'.")] = "per_directory",
    backend: Annotated[str, typer.Option("--backend", help="'ollama' or 'gemini'.")] = "ollama",
    model: Annotated[str, typer.Option("--model", "-m", help="LLM model.")] = "gemma3:12b",
):
    """[bold cyan]Relate[/bold cyan] — LLM-group files that are conceptually one thing (CAD + backups + exports, etc.)."""
    _init_db(db)
    _init_ai(backend=backend, text_model=model, vision_model=model)
    console.print(Panel("Finding related file groups", style="cyan"))

    from datahoarder.core.relate import relate as do_relate
    from datahoarder.db.models import UserSession
    from datahoarder.db.session import get_engine
    from sqlalchemy.orm import Session as _Session

    # Resolve session
    if not session_id:
        with _Session(get_engine()) as s:
            latest = (
                s.query(UserSession).order_by(UserSession.updated_at.desc()).first()
            )
            if not latest:
                console.print("[red]No sessions found. Run `datahoarder scan` first.[/red]")
                raise typer.Exit(1)
            session_id = latest.id
            console.print(f"Using latest session: [cyan]{session_id}[/cyan]")

    def _cb(d: dict) -> None:
        console.print(
            f"  [dim]{d['done']}/{d['total']}[/dim]  "
            f"[bold]{d['groups']}[/bold] groups so far "
            f"([green]{d['llm_groups']} LLM[/green] + "
            f"[yellow]{d['backstop_groups']} backstop[/yellow])"
        )

    summary = do_relate(
        session_id=session_id, scope=scope, model=model, progress_cb=_cb,
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
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
    limit: Annotated[Optional[int], typer.Option("--limit", help="Max proposals to generate.")] = None,
    offset: Annotated[Optional[int], typer.Option("--offset", help="Skip first N files.")] = None,
):
    """[bold blue]Generate[/bold blue] rename/tag proposals from analyzed files."""
    _init_db(db)
    console.print(Panel("Generating proposals", style="blue"))

    from datahoarder.proposals.namer import generate_proposals
    counts = generate_proposals(limit=limit, offset=offset)

    console.print(
        f"\n[bold green]Proposals ready[/bold green] — "
        f"{counts['rename']} renames, "
        f"{counts['tags']} tag updates, "
        f"{counts['skipped']} unchanged"
    )
    console.print("Run [bold]datahoarder review[/bold] to inspect them.")


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

@app.command()
def review(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
    min_confidence: Annotated[float, typer.Option("--min-confidence", "-c")] = 0.0,
    dupes: Annotated[bool, typer.Option("--dupes", help="Show duplicate groups instead of rename proposals.")] = False,
    interactive: Annotated[bool, typer.Option("--interactive", "-i", help="Approve/reject one by one.")] = False,
    limit: Annotated[Optional[int], typer.Option("--limit", help="Max proposals to display.")] = None,
    offset: Annotated[Optional[int], typer.Option("--offset", help="Skip first N proposals.")] = None,
):
    """[bold]Review[/bold] pending proposals before applying them."""
    _init_db(db)

    if dupes:
        _review_dupes(limit=limit, offset=offset)
        return

    from datahoarder.executor import preview
    preview(min_confidence=min_confidence, limit=limit, offset=offset)

    if interactive:
        _interactive_review(limit=limit, offset=offset)
    else:
        console.print(
            "\nRun [bold]datahoarder execute[/bold] to apply all high-confidence proposals, "
            "or [bold]datahoarder execute --commit[/bold] to apply for real."
        )


def _review_dupes(
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> None:
    from datahoarder.core.dedup import duplicate_summary

    groups = duplicate_summary()
    if not groups:
        console.print("[yellow]No duplicate groups found. Run 'datahoarder dedup' first.[/yellow]")
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
) -> None:
    """One-by-one proposal review loop."""
    from datahoarder.db.session import get_engine
    from datahoarder.db.models import Proposal, ProposalStatus
    from sqlalchemy.orm import Session

    engine = get_engine()

    with Session(engine) as session:
        query = (
            session.query(Proposal)
            .filter(Proposal.status == ProposalStatus.PENDING)
            .order_by(Proposal.confidence.desc())
        )
        total = query.count()
        if offset:
            query = query.offset(offset)
        if limit:
            query = query.limit(limit)
        proposals = query.all()

    console.print(f"\n[bold]{len(proposals)} proposals to review[/bold] (total pending: {total}).")
    console.print("Keys: [green]y[/green]=approve  [red]n[/red]=reject  [yellow]s[/yellow]=skip  [bold]q[/bold]=quit\n")

    for i, prop in enumerate(proposals, 1):
        console.print(
            f"[dim]{i}/{len(proposals)}[/dim]  "
            f"[cyan]{prop.proposal_type.value}[/cyan]  "
            f"conf={prop.confidence:.0%}  "
            f"[red]{Path(prop.current_value or '').name}[/red] -&gt; "
            f"[green]{Path(prop.proposed_value or '').name}[/green]"
        )
        if prop.reasoning:
            console.print(f"  [dim]{prop.reasoning[:100]}[/dim]")

        choice = typer.prompt("  Action", default="s")
        with Session(engine) as session:
            p = session.get(Proposal, prop.id)
            if choice.lower() == "y":
                p.status = ProposalStatus.APPROVED
                console.print("  [green]Approved[/green]")
            elif choice.lower() == "n":
                p.status = ProposalStatus.REJECTED
                console.print("  [red]Rejected[/red]")
            elif choice.lower() == "q":
                session.commit()
                break
            session.commit()


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

@app.command()
def execute(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
    commit: Annotated[bool, typer.Option("--commit", help="Apply changes for real (default is dry-run).")] = False,
    min_confidence: Annotated[float, typer.Option("--min-confidence", "-c", help="Only apply proposals above this confidence.")] = 0.7,
):
    """
    [bold red]Execute[/bold red] proposals on disk.

    Defaults to dry-run. Pass [bold]--commit[/bold] to make real changes.

    All changes are logged to ~/.datahoarder/undo.log for recovery.
    Use [bold]datahoarder undo --last[/bold] to reverse recent operations.
    """
    _init_db(db)

    from datahoarder.executor import execute as do_execute

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
    )


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------

@app.command()
def undo(
    last: Annotated[bool, typer.Option("--last", help="Undo the most recent batch of operations.")] = True,
    session_id: Annotated[Optional[str], typer.Option("--session", "-s", help="Undo operations from a specific session.")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation prompt.")] = False,
    list_sessions: Annotated[bool, typer.Option("--list", "-l", help="List available undo sessions.")] = False,
):
    """[bold red]Undo[/bold red] the last executed operations (requires --force to apply without confirmation)."""
    from datahoarder.core.undo_log import undo_operations, list_undo_sessions

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

    if not force:
        console.print(Panel("[bold yellow]DRY RUN — use --force to actually undo[/bold yellow]", style="yellow"))

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
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
):
    """Show database statistics and progress summary."""
    _init_db(db)

    from datahoarder.db.session import get_engine
    from datahoarder.db.models import File, Proposal, ProposalStatus, DuplicateGroup
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

    table = Table(title="DataHoarder Stats", show_lines=True)
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
def pipeline(
    root: Annotated[Path, typer.Argument(help="Directory to process end-to-end.")],
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
    backend: Annotated[str, typer.Option("--backend", help="AI backend: ollama|gemini|auto", envvar="DATAHOARDER_BACKEND")] = "ollama",
    ollama_host: Annotated[str, typer.Option("--ollama-host", help="Ollama server URL.", envvar="OLLAMA_HOST")] = "http://localhost:11434",
    model: Annotated[str, typer.Option("--model", help="Model name.", envvar="DATAHOARDER_MODEL")] = "gemma3:12b",
    workers: Annotated[int, typer.Option("--workers", "-w")] = 1,
    skip_analyze: Annotated[bool, typer.Option("--skip-analyze")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = True,
):
    """
    [bold]Run the full pipeline[/bold]: scan -&gt; enrich -&gt; dedup -&gt; analyze -&gt; propose -&gt; (preview).

    Does NOT execute changes unless you follow up with [bold]datahoarder execute --commit[/bold].
    """
    _init_db(db)
    root = root.resolve()
    console.print(Panel(f"[bold]Full pipeline on:[/bold] {root}", style="bold cyan"))

    # scan
    from datahoarder.core.scanner import scan as do_scan
    console.print("\n[bold cyan]Step 1/5: Scanning…[/bold cyan]")
    do_scan(root)

    # enrich
    from datahoarder.core.enricher import enrich as do_enrich
    console.print("\n[bold green]Step 2/5: Enriching…[/bold green]")
    do_enrich()

    # dedup
    from datahoarder.core.dedup import find_exact_duplicates, find_perceptual_duplicates
    console.print("\n[bold yellow]Step 3/5: Deduplicating…[/bold yellow]")
    find_exact_duplicates()
    find_perceptual_duplicates()

    # analyze
    if not skip_analyze:
        console.print("\n[bold magenta]Step 4/5: Analyzing with AI…[/bold magenta]")
        try:
            _init_ai(backend, ollama_host, model)
            from datahoarder.analyzers.pipeline import analyze as do_analyze
            do_analyze(workers=workers)
        except RuntimeError as exc:
            console.print(f"[yellow]AI analysis skipped: {exc}[/yellow]")
    else:
        console.print("\n[dim]Step 4/5: AI analysis skipped (--skip-analyze)[/dim]")

    # propose
    console.print("\n[bold blue]Step 5/5: Generating proposals…[/bold blue]")
    from datahoarder.proposals.namer import generate_proposals
    generate_proposals()

    # summary
    console.print("\n")
    stats(db=db)

    console.print(
        Panel(
            "[bold green]Pipeline complete![/bold green]\n\n"
            "Next steps:\n"
            "  • [cyan]datahoarder review[/cyan]               — inspect rename proposals\n"
            "  • [cyan]datahoarder review --dupes[/cyan]        — inspect duplicate groups\n"
            "  • [cyan]datahoarder review --interactive[/cyan]  — approve one by one\n"
            "  • [cyan]datahoarder execute --commit[/cyan]      — apply approved changes\n",
            style="green",
        )
    )


# ---------------------------------------------------------------------------
# serve (web UI)
# ---------------------------------------------------------------------------

@app.command()
def serve(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
    host: Annotated[str, typer.Option("--host", help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port number.")] = 8080,
):
    """[bold]Launch[/bold] the web review UI in your browser."""
    import uvicorn
    from datahoarder.web.app import create_app

    db_path = Path(db)
    web_app = create_app(db_path)

    console.print(
        Panel(
            f"[bold green]DataHoarder Web UI[/bold green]\n\n"
            f"  Open [cyan]http://{host}:{port}[/cyan] in your browser\n"
            f"  Database: [dim]{db_path.resolve()}[/dim]\n"
            f"  Press Ctrl+C to stop",
            style="green",
        )
    )

    uvicorn.run(web_app, host=host, port=port, log_level="warning")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@app.command()
def config(
    edit_naming: Annotated[bool, typer.Option("--edit-naming", help="Open naming_rules.json in your default editor.")] = False,
    reset_naming: Annotated[bool, typer.Option("--reset-naming", help="Reset naming_rules.json to built-in defaults.")] = False,
):
    """[bold]Manage[/bold] DataHoarder configuration files."""
    from datahoarder.config import (
        _DEFAULT_NAMING_RULES_FILE,
        load_naming_rules,
        save_naming_rules,
        _DEFAULT_USELESS_STEM_PATTERNS,
        _DEFAULT_HYGIENE_CONFIG,
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
            # Seed with defaults so user has something to edit
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()

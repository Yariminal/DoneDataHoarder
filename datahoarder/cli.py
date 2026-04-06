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

import io
import os
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

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
# scan
# ---------------------------------------------------------------------------

@app.command()
def scan(
    root: Annotated[Path, typer.Argument(help="Directory to scan.")],
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
    force: Annotated[bool, typer.Option("--force", help="Re-scan already-indexed files.")] = False,
):
    """[bold cyan]Scan[/bold cyan] a directory and build the file index."""
    _init_db(db)
    root = root.resolve()
    if not root.exists():
        console.print(f"[red]Path does not exist: {root}[/red]")
        raise typer.Exit(1)

    console.print(Panel(f"Scanning [bold]{root}[/bold]", style="cyan"))

    from datahoarder.core.scanner import scan as do_scan
    counts = do_scan(root, force_rescan=force)

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
        console.print(f"Run [bold]datahoarder review --dupes[/bold] to inspect groups.")


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------

@app.command()
def propose(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
    limit: Annotated[Optional[int], typer.Option("--limit")] = None,
):
    """[bold blue]Generate[/bold blue] rename/tag proposals from analyzed files."""
    _init_db(db)
    console.print(Panel("Generating proposals", style="blue"))

    from datahoarder.proposals.namer import generate_proposals
    counts = generate_proposals(limit=limit)

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
):
    """[bold]Review[/bold] pending proposals before applying them."""
    _init_db(db)

    if dupes:
        _review_dupes()
        return

    from datahoarder.executor import preview
    preview(min_confidence=min_confidence)

    if interactive:
        _interactive_review()
    else:
        console.print(
            "\nRun [bold]datahoarder execute[/bold] to apply all high-confidence proposals, "
            "or [bold]datahoarder execute --commit[/bold] to apply for real."
        )


def _review_dupes() -> None:
    from datahoarder.core.dedup import duplicate_summary

    groups = duplicate_summary()
    if not groups:
        console.print("[yellow]No duplicate groups found. Run 'datahoarder dedup' first.[/yellow]")
        return

    for g in groups[:50]:  # show first 50
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


def _interactive_review() -> None:
    """One-by-one proposal review loop."""
    from datahoarder.db.session import get_engine
    from datahoarder.db.models import Proposal, ProposalStatus
    from sqlalchemy.orm import Session

    engine = get_engine()

    with Session(engine) as session:
        proposals = (
            session.query(Proposal)
            .filter(Proposal.status == ProposalStatus.PENDING)
            .order_by(Proposal.confidence.desc())
            .all()
        )

    console.print(f"\n[bold]{len(proposals)} proposals to review.[/bold]")
    console.print("Keys: [green]y[/green]=approve  [red]n[/red]=reject  [yellow]s[/yellow]=skip  [bold]q[/bold]=quit\n")

    from datahoarder.db.session import get_engine
    engine = get_engine()

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
    approved_only: Annotated[bool, typer.Option("--approved-only", help="Only apply explicitly approved proposals.")] = False,
):
    """
    [bold red]Execute[/bold red] proposals on disk.

    Defaults to dry-run. Pass [bold]--commit[/bold] to make real changes.
    """
    _init_db(db)

    from datahoarder.executor import execute as do_execute
    from datahoarder.db.models import ProposalStatus

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
# stats
# ---------------------------------------------------------------------------

@app.command()
def stats(
    db: Annotated[str, typer.Option("--db", help="SQLite database path.", envvar="DATAHOARDER_DB")] = "datahoarder.db",
):
    """Show database statistics and progress summary."""
    _init_db(db)

    from datahoarder.db.session import get_engine
    from datahoarder.db.models import File, FileStatus, Proposal, ProposalStatus, DuplicateGroup
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()

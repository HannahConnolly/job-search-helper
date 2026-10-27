"""Command-line interface; see `jobhelper --help`."""

from __future__ import annotations

import json
import re
import webbrowser
from datetime import datetime
from sqlite3 import Row
from typing import Annotated

import anthropic
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape

from . import llm, pipeline, profile
from .config import Config, ConfigError, load_config
from .db import ACTIVE, JobStore, Status, now
from .display import fit_text, jobs_table, local_date
from .filters import phrase_pattern

app = typer.Typer(
    help="LoopCV-style job search: find jobs, rank them against your resume with Claude, and prep applications.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,  # locals can include API keys
)
console = Console()
errors = Console(stderr=True)

JobId = Annotated[int, typer.Argument(help="Job ID from `jobhelper list`.")]


def _open() -> tuple[Config, JobStore]:
    cfg = load_config()
    return cfg, JobStore(cfg.data_dir / "jobs.db")


def _job(store: JobStore, job_id: int) -> Row:
    row = store.get(job_id)
    if row is None:
        errors.print(f"[red]No job #{job_id}.[/] `jobhelper list --all` shows every job.")
        raise typer.Exit(1)
    return row


def _table(cfg: Config, rows: list[Row], title: str):
    # For multi-office jobs, show the office that matched your locations.
    return jobs_table(rows, title, prefer=phrase_pattern([*cfg.filters.locations, "remote"]))


def _sync(cfg: Config) -> None:
    with console.status("Downloading your documents…"):
        changed = profile.sync(cfg)
    for name, updated in changed.items():
        console.print(f"  {name}: {'updated' if updated else 'unchanged'} → data/{name}.txt")


@app.command()
def sync() -> None:
    """Download your resume and behavioral doc (the Google Docs linked in README.md) into data/."""
    _sync(load_config())


@app.command()
def search(
    loop: Annotated[list[str] | None, typer.Option("--loop", "-l", help="Run only this loop (repeatable).")] = None,
    jobspy: Annotated[bool, typer.Option(help="Run the JobSpy loops (Indeed, LinkedIn, ...).")] = True,
    boards: Annotated[bool, typer.Option(help="Read the company job boards.")] = True,
) -> None:
    """Run your saved searches and company boards; store new jobs that pass the filters."""
    cfg, store = _open()
    if unknown := set(loop or []) - {entry.name for entry in cfg.loops}:
        raise typer.BadParameter(f"no loop named {', '.join(sorted(unknown))}", param_hint="--loop")
    pipeline.search(cfg, store, profile.load(cfg), console, loop, use_jobspy=jobspy, use_boards=boards)


@app.command()
def rank(
    limit: Annotated[
        int | None, typer.Option(help="Score at most this many jobs (default: max_jobs_scored_per_run).")
    ] = None,
    rescore: Annotated[
        bool, typer.Option(help="Re-score new and saved jobs that already have a score, e.g. after editing your resume.")
    ] = False,
) -> None:
    """Score unscored jobs against your resume and preferences with Claude."""
    cfg, store = _open()
    pipeline.rank(cfg, store, profile.load(cfg), console, limit=limit, rescore=rescore)


@app.command()
def run(
    limit: Annotated[int | None, typer.Option(help="Score at most this many jobs.")] = None,
    sync_docs: Annotated[bool, typer.Option("--sync/--no-sync", help="Re-download your documents first.")] = True,
) -> None:
    """The whole loop: sync documents, search, score new jobs, write a digest. Schedule this daily."""
    cfg, store = _open()
    started = now()
    if sync_docs:
        try:
            _sync(cfg)
        except profile.ProfileError as e:
            # A scheduled run shouldn't die because Google Docs blipped; use the last copies.
            if not all((cfg.data_dir / f"{name}.txt").is_file() for name in profile.DOCS):
                raise
            errors.print(f"[yellow]Using the last synced documents:[/] {escape(str(e))}")
    prof = profile.load(cfg)
    pipeline.search(cfg, store, prof, console)
    try:
        pipeline.rank(cfg, store, prof, console, limit=limit)
    except llm.LLMError as e:
        errors.print(f"[yellow]Skipped scoring:[/] {escape(str(e))}")
    except anthropic.AuthenticationError:
        errors.print("[yellow]Skipped scoring:[/] Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.")
    path = pipeline.write_digest(cfg, store, started)
    console.print(_table(cfg, store.scored_since(started, cfg.llm.min_score), f"New matches scored {cfg.llm.min_score}+"))
    console.print(f"Digest → {path.relative_to(cfg.root)}")


@app.command("list")
def list_jobs(
    status: Annotated[Status | None, typer.Option(help="Only jobs with this status (any score).")] = None,
    min_score: Annotated[int | None, typer.Option(help="Minimum fit score (default: min_score in config.toml).")] = None,
    show_all: Annotated[bool, typer.Option("--all", help="Every job, including unscored and low scores.")] = False,
    limit: Annotated[int, typer.Option(help="How many rows to show.")] = 30,
) -> None:
    """Your best matches: new and saved jobs at or above the score cutoff."""
    cfg, store = _open()
    if status:
        statuses, cutoff, title = [status.value], min_score, f"{status.value.title()} jobs"
    elif show_all:
        statuses, cutoff, title = None, min_score, "All jobs"
    else:
        cutoff = cfg.llm.min_score if min_score is None else min_score
        statuses, title = list(ACTIVE), f"Best matches (score {cutoff}+)"
    console.print(_table(cfg, store.listing(statuses, cutoff, limit), title))
    counts = " · ".join(f"{name} {count}" for name, count in store.status_counts().items() if count)
    console.print(f"[dim]{counts or 'no jobs yet'} · {store.count_unscored()} unscored[/]")


@app.command()
def show(job_id: JobId, full: Annotated[bool, typer.Option(help="Print the whole description.")] = False) -> None:
    """One job in full: details, Claude's assessment, your notes, and the description."""
    cfg, store = _open()
    row = _job(store, job_id)
    console.rule(escape(f"#{row['id']}  {row['title']} · {row['company']}"))
    facts = [
        row["location"],
        {1: "remote", 0: "not remote"}.get(row["is_remote"]),
        row["salary"],
        f"posted {row['date_posted']}" if row["date_posted"] else None,
        f"found {local_date(row['first_seen'])} via {row['source']}",
    ]
    console.print(escape(" · ".join(fact for fact in facts if fact)))
    console.print(f"Status: [bold]{row['status']}[/]")
    console.print(f"Posting: {escape(row['url'])}", soft_wrap=True)
    if row["apply_url"] and row["apply_url"] != row["url"]:
        console.print(f"Apply:   {escape(row['apply_url'])}", soft_wrap=True)
    if row["notes"]:
        console.print("Notes:\n" + escape(row["notes"].rstrip()))
    if row["score"] is not None:
        assessment = json.loads(row["assessment"])
        console.print(f"\nFit {fit_text(row['score'])}/100: {escape(assessment['summary'])}")
        for label, key in (("Strengths", "strengths"), ("Gaps", "gaps"), ("Dealbreakers", "dealbreakers")):
            if assessment.get(key):
                console.print(f"[bold]{label}[/]")
                for item in assessment[key]:
                    console.print(f"  • {escape(item)}")
        if assessment.get("years_required"):
            console.print(f"[dim]Asks for {assessment['years_required']}+ years of experience.[/]")
    elif row["assessment"]:
        error = json.loads(row["assessment"]).get("error", "")
        console.print(f"\n[yellow]Claude couldn't score this job:[/] {escape(error)} (`jobhelper rank --rescore` retries)")
    else:
        console.print("\n[dim]Not scored yet; run `jobhelper rank`.[/]")
    description = row["description"].strip() or "(no description)"
    if not full and len(description) > 1500:
        description = description[:1500].rsplit("\n", 1)[0] + "\n\n*… use --full for the rest*"
    console.print()
    console.print(Markdown(description))


@app.command()
def prep(job_id: JobId) -> None:
    """Tailored packet for one job: resume edits, cover letter, interview plan, outreach note."""
    cfg, store = _open()
    row = _job(store, job_id)
    prof = profile.load(cfg)
    client = llm.make_client()
    usage = llm.Usage()
    with console.status(f"Writing your prep packet with {cfg.llm.writer_model} (a minute or two)…"):
        text = llm.write_prep(
            client, cfg.llm.writer_model, cfg.llm.writer_effort, prof.prompt_block(), pipeline.prep_request(row), usage
        )
    path = cfg.output_dir / "prep" / f"{row['id']}-{_slug(row['company'])}-{_slug(row['title'])}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {row['title']} at {row['company']}\n\n{row['apply_url'] or row['url']}\n\n{text}\n")
    console.print(Markdown(_first_section(text)))
    console.print(f"\nFull packet → {path.relative_to(cfg.root)}")
    if row["status"] == Status.new.value:
        store.set_status(row["id"], Status.saved.value)
        console.print("[dim]Marked as saved.[/]")
    console.print(f"[dim]{usage.summary()}[/]")


@app.command()
def resume(
    job_id: Annotated[int | None, typer.Argument(help="Tailor to this job (from `jobhelper list`).")] = None,
    sync_docs: Annotated[bool, typer.Option("--sync/--no-sync", help="Re-download your documents first.")] = True,
) -> None:
    """Rewrite your resume: polished, or tailored to one job (writes output/resume/...md)."""
    cfg, store = _open()
    row = _job(store, job_id) if job_id is not None else None
    if sync_docs:
        _sync(cfg)
    prof = profile.load(cfg)
    client = llm.make_client()
    usage = llm.Usage()
    if row:
        request, name = f"{pipeline.job_block(row)}\n\nRewrite my resume for this role.", (
            f"{row['id']}-{_slug(row['company'])}-{_slug(row['title'])}"
        )
    else:
        request, name = "Rewrite my resume.", f"general-{datetime.now():%Y-%m-%d}"
    with console.status(f"Rewriting your resume with {cfg.llm.writer_model} (a minute or two)…"):
        text = llm.write_resume(client, cfg.llm.writer_model, cfg.llm.writer_effort, prof.prompt_block(), request, usage)
    path = cfg.output_dir / "resume" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")
    changes = text.split("## What changed", 1)
    if len(changes) == 2:
        console.print(Markdown("## What changed" + changes[1]))
    console.print(f"\nRewritten resume → {path.relative_to(cfg.root)}")
    placeholders = text.count("[add:")
    if placeholders:
        console.print(f"[yellow]{placeholders} {escape('[add: …]')} placeholder(s) to fill in before you use it.[/]")
    console.print(f"[dim]{usage.summary()}[/]")


@app.command()
def review(
    sync_docs: Annotated[bool, typer.Option("--sync/--no-sync", help="Re-download your documents first.")] = True,
) -> None:
    """Critique your resume and tighten every behavioral answer (writes output/review-DATE.md)."""
    cfg = load_config()
    if sync_docs:
        _sync(cfg)
    prof = profile.load(cfg)
    client = llm.make_client()
    usage = llm.Usage()
    with console.status(f"Reviewing your documents with {cfg.llm.writer_model} (a few minutes)…"):
        text = llm.write_review(client, cfg.llm.writer_model, cfg.llm.writer_effort, prof.prompt_block(), usage)
    path = cfg.output_dir / f"review-{datetime.now():%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Resume and interview review\n\n{text}\n")
    console.print(Markdown(_first_section(text)))
    console.print(f"\nFull review → {path.relative_to(cfg.root)}")
    console.print(f"[dim]{usage.summary()}[/]")


@app.command()
def mark(
    job_id: JobId,
    status: Annotated[Status, typer.Argument(help="New status.")],
    note: Annotated[str, typer.Option("--note", "-n", help="Add a dated note, e.g. who you spoke with.")] = "",
) -> None:
    """Move a job through your pipeline: saved, applied, interviewing, offer, rejected, skipped."""
    cfg, store = _open()
    row = _job(store, job_id)
    store.set_status(row["id"], status.value, note)
    console.print(escape(f"#{row['id']} {row['title']} · {row['company']}: {row['status']} → {status.value}"))


@app.command("open")
def open_job(job_id: JobId) -> None:
    """Open the job's apply link in your browser."""
    cfg, store = _open()
    row = _job(store, job_id)
    url = row["apply_url"] or row["url"]
    console.print(escape(url), soft_wrap=True)
    webbrowser.open(url)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "job"


def _first_section(markdown: str) -> str:
    starts = [match.start() for match in re.finditer(r"^## ", markdown, re.MULTILINE)]
    return markdown[: starts[1]] if len(starts) > 1 else markdown


def main() -> None:
    try:
        app()
    except (ConfigError, profile.ProfileError, llm.LLMError) as e:
        errors.print(f"[red]Error:[/] {escape(str(e))}")
        raise SystemExit(1) from None
    except anthropic.AuthenticationError:
        errors.print("[red]Error:[/] Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.")
        raise SystemExit(1) from None
    except anthropic.APIStatusError as e:
        errors.print(f"[red]Claude API error {e.status_code}:[/] {escape(e.message)}")
        raise SystemExit(1) from None
    except anthropic.APIConnectionError:
        errors.print("[red]Error:[/] Couldn't reach the Claude API. Check your connection.")
        raise SystemExit(1) from None

"""The loop: search every source, filter, store, score with Claude, and write a digest."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from sqlite3 import Row

import anthropic
import pydantic
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import llm
from .config import Config
from .db import JobStore, now
from .display import fit_text
from .filters import JobFilter, KeywordScorer
from .jobs import html_to_text
from .profile import Profile
from .sources import SourceResult, boards, jobspy_source, loopcv

# Failures tied to one posting (a refusal, unusable output); retrying won't help.
JOB_ERRORS = (llm.LLMError, pydantic.ValidationError)


def search(
    cfg: Config,
    store: JobStore,
    profile: Profile,
    console: Console,
    loop_names: list[str] | None = None,
    use_jobspy: bool = True,
    use_boards: bool = True,
) -> int:
    """Run the searches and store new jobs that pass the filters; returns how many were new."""
    loops = [loop for loop in cfg.loops if not loop_names or loop.name in loop_names]
    results: list[SourceResult] = []
    if use_jobspy:
        for loop in loops:
            with console.status(escape(f"Searching {loop.name}: {loop.search} · {loop.location}…")):
                results.append(jobspy_source.run_loop(loop, cfg.search))
    if api_key := os.environ.get("LOOPCV_API_KEY"):
        with console.status("Fetching LoopCV matches…"):
            results += loopcv.run_loops(loops, cfg.loopcv, store, api_key)
    if use_boards:
        with console.status("Reading company job boards…"):
            results += boards.fetch_all(cfg.companies)
    return _store(results, cfg, store, profile, console)


def _store(results: list[SourceResult], cfg: Config, store: JobStore, profile: Profile, console: Console) -> int:
    job_filter = JobFilter(cfg.filters)
    keywords = KeywordScorer(profile.keywords)
    seen_at = now()
    counts: dict[str, list[int]] = {}  # source -> [found, passed filters, new]
    problems = []
    board_count = sum(1 for result in results if result.name.split(":")[0] in boards.URLS)
    for result in results:
        group = f"company boards ({board_count})" if result.name.split(":")[0] in boards.URLS else result.name
        tally = counts.setdefault(group, [0, 0, 0])
        tally[0] += len(result.jobs)
        if result.error:
            problems.append(f"{result.name}: {result.error}")
        for job in result.jobs:
            if job_filter.reject_reason(job, check_location=not result.location_targeted):
                continue
            if job.description_html and not job.description:
                job.description = html_to_text(job.description_html)
            tally[1] += 1
            tally[2] += store.upsert(job, keywords.hits(f"{job.title}\n{job.description}"), seen_at)
    store.commit()

    table = Table("Source", "Found", "Passed filters", "New", title="Search", title_justify="left")
    for name, (found, passed, new) in counts.items():
        table.add_row(escape(name), str(found), str(passed), f"[bold]{new}[/]" if new else "0")
    console.print(table)
    for problem in problems:
        console.print(f"[yellow]⚠ {escape(problem)}[/]")
    return sum(new for _, _, new in counts.values())


def rank(
    cfg: Config, store: JobStore, profile: Profile, console: Console, limit: int | None = None, rescore: bool = False
) -> None:
    """Score jobs against the profile with Claude, best keyword matches first."""
    rows = store.to_score(limit or cfg.llm.max_jobs_scored_per_run, rescore)
    if not rows:
        console.print("No jobs waiting to be scored.")
        return
    client = llm.make_client()
    usage = llm.Usage()
    model, effort = cfg.llm.score_model, cfg.llm.score_effort
    profile_block = profile.prompt_block()

    def score(row: Row) -> llm.FitAssessment:
        return llm.score_job(client, model, effort, profile_block, job_block(row), usage)

    failed = 0
    columns = (TextColumn(f"Scoring with {model}"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn())
    with Progress(*columns, console=console) as progress:
        task = progress.add_task("score", total=len(rows))

        def record(row: Row, assessment: llm.FitAssessment) -> None:
            store.save_score(row["id"], assessment.score, assessment.model_dump())
            progress.console.print(
                f"  {fit_text(assessment.score)}  {escape(row['title'])} · {escape(row['company'])} [dim]#{row['id']}[/]"
            )
            progress.advance(task)

        def fail(row: Row, error: Exception, retry: bool) -> None:
            nonlocal failed
            failed += 1
            if not retry:
                store.save_failure(row["id"], str(error))
            progress.console.print(f"  [red]failed[/] #{row['id']} {escape(row['title'])}: {escape(str(error))}")
            progress.advance(task)

        # The first call runs alone: it writes the prompt cache the parallel calls then read, and
        # an API problem (bad key, unknown model) stops the run before anything fans out.
        try:
            record(rows[0], score(rows[0]))
        except JOB_ERRORS as e:
            fail(rows[0], e, retry=False)
        pool = ThreadPoolExecutor(max_workers=cfg.llm.concurrency)
        futures = {pool.submit(score, row): row for row in rows[1:]}
        try:
            for future in as_completed(futures):
                row = futures[future]
                try:
                    record(row, future.result())
                except JOB_ERRORS as e:
                    fail(row, e, retry=False)
                except anthropic.APIError as e:  # overloaded, rate-limited, network: try again next run
                    fail(row, e, retry=True)
        finally:
            pool.shutdown(cancel_futures=True)  # after Ctrl-C, don't start (or pay for) the rest

    console.print(f"[dim]{usage.summary()}[/]")
    if failed:
        console.print(f"[yellow]{failed} job(s) couldn't be scored (see above); `jobhelper rank --rescore` retries all.[/]")
    if waiting := store.count_unscored():
        console.print(f"{waiting} job(s) still unscored: run [bold]jobhelper rank[/] again, or pass --limit.")


def job_block(row: Row) -> str:
    remote = {1: "yes", 0: "no"}.get(row["is_remote"], "not stated")
    facts = [
        f"Title: {row['title']}",
        f"Company: {row['company']}",
        f"Location: {row['location'] or 'not stated'}",
        f"Remote: {remote}",
    ]
    if row["salary"]:
        facts.append(f"Salary: {row['salary']}")
    if row["date_posted"]:
        facts.append(f"Posted: {row['date_posted']}")
    description = row["description"].strip() or "(The listing has no description; judge from the title and company.)"
    return "<job_posting>\n" + "\n".join(facts) + f"\n\n{description}\n</job_posting>"


def prep_request(row: Row) -> str:
    parts = [job_block(row)]
    if row["score"] is not None:
        assessment = json.loads(row["assessment"])
        notes = [f"Fit score {row['score']}/100: {assessment['summary']}"]
        notes += [f"Strength: {item}" for item in assessment["strengths"]]
        notes += [f"Gap: {item}" for item in assessment["gaps"]]
        notes += [f"Dealbreaker: {item}" for item in assessment["dealbreakers"]]
        parts.append("<screening_notes>\n" + "\n".join(notes) + "\n</screening_notes>")
    parts.append("Write my prep packet for this role.")
    return "\n\n".join(parts)


def write_digest(cfg: Config, store: JobStore, since: str) -> Path:
    """Markdown summary of the jobs found since `since` that scored at or above min_score."""
    rows = store.scored_since(since, cfg.llm.min_score)
    stamp = datetime.now()
    lines = [
        f"# Job digest · {stamp:%a %b} {stamp.day}, {stamp:%Y %H:%M}",
        "",
        f"{store.found_since(since)} new job(s) this run · {len(rows)} scored {cfg.llm.min_score}+ · "
        f"{store.count_unscored()} waiting to be scored",
        "",
    ]
    for row in rows:
        assessment = json.loads(row["assessment"])
        link = f"[posting]({row['apply_url'] or row['url']})"
        meta = " · ".join(filter(None, [row["location"], row["salary"], link, f"`jobhelper prep {row['id']}`"]))
        lines += [f"## {row['score']} · {row['title']} at {row['company']}", "", meta, "", assessment["summary"], ""]
        if assessment["strengths"]:
            lines.append("- **Fit:** " + "; ".join(assessment["strengths"][:3]))
        if assessment["gaps"]:
            lines.append("- **Gaps:** " + "; ".join(assessment["gaps"][:3]))
        if assessment["dealbreakers"]:
            lines.append("- **Blockers:** " + "; ".join(assessment["dealbreakers"]))
        lines.append("")
    path = cfg.output_dir / "digests" / f"{stamp:%Y-%m-%d-%H%M}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path

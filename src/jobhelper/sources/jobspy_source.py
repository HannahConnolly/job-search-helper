"""Saved searches on Indeed, LinkedIn, Google Jobs and others through JobSpy.

JobSpy scrapes the boards' public search pages. That is fine for a personal search at modest
volume, but the boards rate-limit (LinkedIn especially) and their layouts change; when one
breaks, the others keep working and the error is shown in the search summary.
"""

from __future__ import annotations

from datetime import date

from ..config import LoopConfig, SearchConfig
from ..jobs import Job, format_salary
from . import SourceResult


def run_loop(loop: LoopConfig, search: SearchConfig) -> SourceResult:
    from jobspy import scrape_jobs  # pulls in pandas; only import it when searching

    name = f"loop:{loop.name}"
    # LinkedIn ignores is_remote, so a remote loop's results still go through the location filter.
    targeted = not loop.remote
    try:
        frame = scrape_jobs(
            site_name=loop.sites or search.sites,
            search_term=loop.search,
            location=loop.location,
            distance=search.distance,
            is_remote=loop.remote,
            results_wanted=loop.results_per_site or search.results_per_site,
            hours_old=loop.hours_old or search.hours_old,
            fetch_description=search.fetch_descriptions,
            country_indeed="usa",
            verbose=0,
        )
    except Exception as e:  # bad arguments raise ValueError; a broken scraper can raise anything
        return SourceResult(name, error=f"{type(e).__name__}: {e}", location_targeted=targeted)

    jobs = [job for row in frame.to_dict("records") if (job := _job(row, loop.name))]
    errors = frame.attrs.get("errors") or {}
    error = "; ".join(f"{site}: {message}" for site, message in errors.items()) or None
    return SourceResult(name, jobs, error=error, location_targeted=targeted)


def _value(value):
    """DataFrame rows use NaN (or pandas.NA) for missing values."""
    try:
        return None if value is None or value != value else value
    except TypeError:  # pandas.NA refuses to be compared
        return None


def _job(row: dict, loop: str) -> Job | None:
    row = {key: _value(value) for key, value in row.items()}
    if not row.get("title") or not row.get("job_url"):
        return None
    posted = row.get("date_posted")
    return Job(
        source=row.get("site") or "jobspy",
        title=str(row["title"]).strip(),
        company=str(row.get("company") or "").strip(),
        location=row.get("location") or "",
        url=row["job_url"],
        apply_url=row.get("job_url_direct"),
        description=row.get("description") or "",
        # JobSpy's False often just means no "remote" keyword was seen, so treat it as unknown.
        is_remote=True if row.get("is_remote") else None,
        salary=format_salary(row.get("min_amount"), row.get("max_amount"), row.get("interval"), row.get("currency")),
        date_posted=posted.isoformat() if isinstance(posted, date) else (str(posted) if posted else None),
        loop=loop,
    )

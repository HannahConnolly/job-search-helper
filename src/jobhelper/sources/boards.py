"""Company job boards through the official public APIs of Greenhouse, Lever and Ashby."""

from __future__ import annotations

import html
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx2

from ..config import CompaniesConfig
from ..jobs import Job, format_salary, html_to_text
from . import SourceResult

URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
}


def fetch_all(companies: CompaniesConfig, client: httpx2.Client | None = None) -> list[SourceResult]:
    boards = [(ats, entry) for ats in URLS for entry in getattr(companies, ats)]
    client = client or httpx2.Client(
        timeout=30, follow_redirects=True, headers={"User-Agent": "job-search-helper"}
    )
    with client, ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda board: fetch_board(client, *board), boards))


def fetch_board(client: httpx2.Client, ats: str, entry: str) -> SourceResult:
    """entry is "slug" or "slug:Display Name"."""
    slug, _, display_name = entry.partition(":")
    slug = slug.strip()
    company = display_name.strip() or slug.replace("-", " ").title()
    name = f"{ats}:{slug}"
    try:
        response = client.get(URLS[ats].format(slug=slug))
        response.raise_for_status()
        jobs = PARSERS[ats](response.json(), slug, company)
    except httpx2.HTTPStatusError as e:
        hint = " (is the slug right?)" if e.response.status_code == 404 else ""
        return SourceResult(name, error=f"HTTP {e.response.status_code}{hint}")
    except (httpx2.HTTPError, ValueError, KeyError, TypeError, AttributeError) as e:
        return SourceResult(name, error=f"{type(e).__name__}: {e}")
    return SourceResult(name, jobs)


def parse_greenhouse(data: dict, slug: str, company: str) -> list[Job]:
    return [
        Job(
            source=f"greenhouse:{slug}",
            title=job["title"].strip(),
            company=job.get("company_name") or company,
            location=(job.get("location") or {}).get("name") or "",
            url=job["absolute_url"],
            # Greenhouse HTML-escapes the HTML content.
            description_html=html.unescape(job.get("content") or ""),
            date_posted=(job.get("first_published") or job.get("updated_at") or "")[:10] or None,
        )
        for job in data.get("jobs", [])
    ]


def parse_lever(data: list, slug: str, company: str) -> list[Job]:
    jobs = []
    for job in data:
        categories = job.get("categories") or {}
        locations = categories.get("allLocations") or [categories.get("location") or ""]
        sections = [job.get("descriptionPlain") or ""]
        sections += [f"{item.get('text', '')}\n{html_to_text(item.get('content', ''))}" for item in job.get("lists") or []]
        sections.append(job.get("additionalPlain") or "")
        pay = job.get("salaryRange") or {}
        jobs.append(Job(
            source=f"lever:{slug}",
            title=job["text"].strip(),
            company=company,
            location="; ".join(loc for loc in locations if loc),
            url=job["hostedUrl"],
            apply_url=job.get("applyUrl"),
            description="\n\n".join(s.strip() for s in sections if s.strip()),
            is_remote=True if job.get("workplaceType") == "remote" else None,
            country=job.get("country") or None,
            salary=format_salary(pay.get("min"), pay.get("max"), pay.get("interval"), pay.get("currency")),
            date_posted=_date_from_ms(job.get("createdAt")),
        ))
    return jobs


def parse_ashby(data: dict, slug: str, company: str) -> list[Job]:
    jobs = []
    for job in data.get("jobs", []):
        if job.get("isListed") is False:
            continue
        locations = [job.get("location") or ""] + [
            extra.get("location") or "" for extra in job.get("secondaryLocations") or []
        ]
        address = (job.get("address") or {}).get("postalAddress") or {}
        pay = job.get("compensation") or {}
        jobs.append(Job(
            source=f"ashby:{slug}",
            title=job["title"].strip(),
            company=company,
            location="; ".join(loc for loc in locations if loc),
            url=job["jobUrl"],
            apply_url=job.get("applyUrl"),
            description=job.get("descriptionPlain") or "",
            description_html=job.get("descriptionHtml") or "",
            is_remote=job.get("isRemote"),
            country=address.get("addressCountry") or None,
            salary=pay.get("scrapeableCompensationSalarySummary") or pay.get("compensationTierSummary"),
            date_posted=(job.get("publishedAt") or "")[:10] or None,
        ))
    return jobs


def _date_from_ms(ms) -> str | None:
    return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat() if ms else None


PARSERS = {"greenhouse": parse_greenhouse, "lever": parse_lever, "ashby": parse_ashby}

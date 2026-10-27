"""Optional: LoopCV's B2B API (https://api-docs.loopcv.com) as one more job source.

Each configured loop is created once as a LoopCV loop, which LoopCV then re-runs several times a
day; every search pulls that loop's matches. Needs LOOPCV_API_KEY in .env.
"""

from __future__ import annotations

import httpx2

from ..config import LoopConfig, LoopcvConfig
from ..db import JobStore
from ..jobs import Job
from . import SourceResult

API = "https://b2b-api.loopcv.com/connect"


def run_loops(
    loops: list[LoopConfig],
    settings: LoopcvConfig,
    store: JobStore,
    api_key: str,
    client: httpx2.Client | None = None,
) -> list[SourceResult]:
    client = client or httpx2.Client(base_url=API, headers={"X-API-KEY": api_key}, timeout=30)
    results = []
    with client:
        for loop in loops:
            if not (location := _location(loop)):
                continue
            name = f"loopcv:{loop.name}"
            try:
                loop_id = store.loopcv_id(loop.name) or _create(client, settings, store, loop, location)
                response = client.get(f"/loop/{loop_id}/matches")
                response.raise_for_status()
                jobs = [job for match in response.json() if (job := _job(match, loop.name))]
            except (httpx2.HTTPError, ValueError, KeyError, TypeError, AttributeError) as e:
                results.append(SourceResult(name, error=f"{type(e).__name__}: {e}"))
                continue
            results.append(SourceResult(name, jobs, location_targeted=True))
    return results


def _location(loop: LoopConfig) -> str | None:
    """LoopCV wants "City, Country"; remote loops need loopcv_location set explicitly."""
    if loop.loopcv_location:
        return loop.loopcv_location
    if loop.remote:
        return None
    return f"{loop.location.split(',')[0].strip()}, United States"


def _create(client: httpx2.Client, settings: LoopcvConfig, store: JobStore, loop: LoopConfig, location: str) -> str:
    body = {
        "title": loop.search,
        "location": location,
        "type": settings.job_type,
        "level": settings.level,
        "externalId": loop.name,
    }
    if settings.scrapers:
        body["scrapers"] = settings.scrapers
    response = client.post("/loop", json=body)
    response.raise_for_status()
    loop_id = response.json()["loopId"]
    store.set_loopcv_id(loop.name, loop_id)
    return loop_id


def _job(match: dict, loop: str) -> Job | None:
    url = match.get("url") or match.get("applyLink")
    if not match.get("title") or not url:
        return None
    return Job(
        source="loopcv",
        title=match["title"].strip(),
        company=(match.get("company") or "").strip(),
        location=match.get("location") or "",
        url=url,
        apply_url=match.get("applyLink"),
        description=match.get("summary") or "",
        date_posted=(match.get("createdAtDate") or "")[:10] or None,
        loop=loop,
    )

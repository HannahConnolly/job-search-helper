import json
from datetime import date

import httpx2

from jobhelper.config import LoopConfig, LoopcvConfig, SearchConfig
from jobhelper.sources import boards, jobspy_source, loopcv


def test_greenhouse_keeps_html_for_later_conversion():
    data = {"jobs": [{
        "title": " Backend Engineer ",
        "company_name": "Acme",
        "location": {"name": "New York, NY"},
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
        "content": "&lt;p&gt;Build &lt;b&gt;APIs&lt;/b&gt;&lt;/p&gt;",
        "first_published": "2026-10-05T10:38:08-04:00",
    }]}
    [job] = boards.parse_greenhouse(data, "acme", "Fallback")
    assert (job.title, job.company, job.location, job.date_posted) == ("Backend Engineer", "Acme", "New York, NY", "2026-10-05")
    assert job.description_html == "<p>Build <b>APIs</b></p>" and job.description == ""


def test_lever_joins_locations_and_sections():
    data = [{
        "text": "Software Engineer",
        "categories": {"location": "Remote", "allLocations": ["Remote", "New York, NY"]},
        "hostedUrl": "https://jobs.lever.co/olo/1",
        "applyUrl": "https://jobs.lever.co/olo/1/apply",
        "descriptionPlain": "Intro",
        "lists": [{"text": "What you'll do", "content": "<li>Ship APIs</li>"}],
        "additionalPlain": "Benefits",
        "workplaceType": "remote",
        "country": "US",
        "createdAt": 1764945549197,
        "salaryRange": {"min": 120000, "max": 150000, "currency": "USD", "interval": "per-year-salary"},
    }]
    [job] = boards.parse_lever(data, "olo", "Olo")
    assert job.location == "Remote; New York, NY"
    assert job.is_remote and job.country == "US"
    assert "What you'll do\n- Ship APIs" in job.description and job.description.endswith("Benefits")
    assert job.salary == "$120,000–$150,000/yr"
    assert job.date_posted == "2025-12-05"


def test_ashby_skips_unlisted_jobs():
    data = {"jobs": [
        {"title": "Backend Engineer", "location": "Remote", "secondaryLocations": [{"location": "New York"}],
         "isRemote": True, "jobUrl": "https://jobs.ashbyhq.com/owner/1", "descriptionPlain": "Text",
         "address": {"postalAddress": {"addressCountry": "United States"}},
         "compensation": {"scrapeableCompensationSalarySummary": "$150K – $190K"}, "publishedAt": "2026-03-17T01:10:34Z"},
        {"title": "Hidden", "isListed": False, "jobUrl": "x"},
    ]}
    [job] = boards.parse_ashby(data, "owner", "Owner")
    assert (job.location, job.country, job.salary) == ("Remote; New York", "United States", "$150K – $190K")


def test_board_errors_are_reported_not_raised():
    client = httpx2.Client(transport=httpx2.MockTransport(lambda request: httpx2.Response(404)))
    result = boards.fetch_board(client, "greenhouse", "nope:Nope Inc")
    assert result.name == "greenhouse:nope" and result.jobs == [] and "slug" in result.error


def test_jobspy_rows_with_missing_values():
    row = {
        "site": "indeed", "title": "Backend Engineer", "company": "Acme", "location": "New York, NY, USA",
        "job_url": "https://www.indeed.com/viewjob?jk=1", "job_url_direct": float("nan"), "description": "Text",
        "is_remote": False, "min_amount": 120000.0, "max_amount": 130000.0, "interval": "yearly", "currency": "USD",
        "date_posted": date(2026, 10, 26),
    }
    job = jobspy_source._job(row, "backend")
    assert job.apply_url is None
    assert job.is_remote is None  # JobSpy's False is a keyword guess, not a fact
    assert (job.salary, job.date_posted, job.loop) == ("$120,000–$130,000/yr", "2026-10-26", "backend")
    assert jobspy_source._job({**row, "title": float("nan")}, "backend") is None
    assert jobspy_source._job({**row, "is_remote": True}, "backend").is_remote is True


def test_remote_loops_are_not_trusted_for_location(monkeypatch):
    import jobspy
    import pandas as pd

    frame = pd.DataFrame([{"site": "linkedin", "title": "Backend Engineer", "company": "Acme",
                           "location": "Durham, NC, USA", "job_url": "https://linkedin.com/jobs/view/1"}])
    frame.attrs = {"errors": {"indeed": "HTTP 429"}, "warnings": {}}
    monkeypatch.setattr(jobspy, "scrape_jobs", lambda **kwargs: frame)

    remote = jobspy_source.run_loop(LoopConfig(name="r", search="backend", location="United States", remote=True), SearchConfig())
    local = jobspy_source.run_loop(LoopConfig(name="nyc", search="backend"), SearchConfig())
    assert not remote.location_targeted and local.location_targeted
    assert remote.error == "indeed: HTTP 429" and remote.jobs[0].location == "Durham, NC, USA"


def test_loopcv_creates_each_loop_once(store):
    calls = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.method, request.url.path, request.headers.get("x-api-key")))
        if request.method == "POST":
            assert json.loads(request.content)["location"] == "New York, United States"
            return httpx2.Response(201, json={"loopId": "abc123"})
        return httpx2.Response(200, json=[{"title": "Backend Engineer", "company": "Acme", "location": "New York",
                                            "url": "https://example.com/1", "applyLink": "https://example.com/1/apply"}])

    loops = [LoopConfig(name="backend", search="backend engineer", location="New York, NY"),
             LoopConfig(name="remote", search="backend engineer", location="United States", remote=True)]

    def run():
        client = httpx2.Client(base_url=loopcv.API, headers={"X-API-KEY": "key"}, transport=httpx2.MockTransport(handle))
        return loopcv.run_loops(loops, LoopcvConfig(), store, "key", client=client)

    [result] = run()  # the remote loop has no loopcv_location, so it's skipped
    assert result.jobs[0].apply_url == "https://example.com/1/apply"
    run()
    assert [c[0] for c in calls] == ["POST", "GET", "GET"]
    assert calls[0] == ("POST", "/connect/loop", "key")
    assert store.loopcv_id("backend") == "abc123"

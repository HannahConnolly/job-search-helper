import io
import json

import httpx2
from rich.console import Console
from typer.testing import CliRunner

from jobhelper import cli, llm, pipeline, profile
from jobhelper.db import JobStore, now
from jobhelper.sources import SourceResult

from .conftest import ASSESSMENT, FakeClaude, add_jobs, make_job, message, sse

QUIET = Console(file=io.StringIO(), width=120)


def test_store_filters_and_converts_html_only_for_kept_jobs(cfg, store):
    kept = make_job(description="", description_html="<p>Build <b>Python</b> APIs</p>")
    senior = make_job(title="Senior Software Engineer", url="https://example.com/s", description="", description_html="<p>x</p>")
    abroad = make_job(title="Backend Engineer", location="London, UK", url="https://example.com/l")
    results = [SourceResult("greenhouse:acme", [kept, senior, abroad])]
    assert pipeline._store(results, cfg, store, profile.load(cfg), QUIET) == 1
    [row] = store.listing(None, None, 10)
    assert row["description"] == "Build **Python** APIs"
    assert row["keyword_hits"] == 1
    assert senior.description == ""  # rejected jobs never pay for conversion


def test_rank_scores_queue_and_respects_limit(cfg, store, monkeypatch):
    add_jobs(store, 5)
    fake = FakeClaude()
    monkeypatch.setattr(llm, "make_client", lambda: fake.client)
    pipeline.rank(cfg, store, profile.load(cfg), QUIET, limit=4)
    assert len(fake.requests) == 4
    assert store.count_unscored() == 1
    scored = store.listing(None, 0, 10)
    assert {row["score"] for row in scored} == {81}
    assert json.loads(scored[0]["assessment"])["gaps"] == ["No Go"]
    # every request carries the same cached system block
    assert len({json.dumps(body["system"]) for _, body in fake.requests}) == 1


def test_rank_records_failures_and_keeps_going(cfg, store, monkeypatch):
    add_jobs(store, 4)
    calls = []

    def respond(body):
        calls.append(body)
        if len(calls) == 2:
            return httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
        return httpx2.Response(200, json=message([{"type": "text", "text": json.dumps(ASSESSMENT)}]))

    fake = FakeClaude(respond)
    monkeypatch.setattr(llm, "make_client", lambda: fake.client)
    pipeline.rank(cfg, store, profile.load(cfg), QUIET)
    assert store.count_unscored() == 1


def test_jobs_claude_cannot_score_are_recorded_not_retried(cfg, store, monkeypatch):
    add_jobs(store, 3)  # "Backend Engineer 2" has the most keyword hits, so it's the first call

    def respond(body):
        if "Backend Engineer 2" in body["messages"][0]["content"]:
            return httpx2.Response(200, json=message([], stop_reason="refusal"))
        return httpx2.Response(200, json=message([{"type": "text", "text": json.dumps(ASSESSMENT)}]))

    fake = FakeClaude(respond)
    monkeypatch.setattr(llm, "make_client", lambda: fake.client)
    pipeline.rank(cfg, store, profile.load(cfg), QUIET)
    assert len(fake.requests) == 3 and store.count_unscored() == 0

    pipeline.rank(cfg, store, profile.load(cfg), QUIET)  # nothing left to pay for
    assert len(fake.requests) == 3

    [refused] = [row for row in store.listing(None, None, 10) if row["score"] is None]
    result = CliRunner().invoke(cli.app, ["show", str(refused["id"])])
    assert "couldn't score this job" in result.output and "declined" in result.output


def test_run_falls_back_to_cached_documents(project, monkeypatch):
    def offline(url, **kwargs):
        raise httpx2.ConnectError("offline")

    monkeypatch.setattr(profile.httpx2, "get", offline)
    monkeypatch.setattr(pipeline, "search", lambda *args, **kwargs: 0)
    result = CliRunner().invoke(cli.app, ["run"])
    assert result.exit_code == 0, result.output
    assert "Using the last synced documents" in result.output
    assert list((project / "output" / "digests").glob("*.md"))


def test_digest_lists_new_jobs_above_cutoff(cfg, store):
    started = now()
    add_jobs(store, 2)
    high, low = store.to_score(limit=10)
    store.save_score(high["id"], 85, ASSESSMENT)
    store.save_score(low["id"], 40, ASSESSMENT)
    text = pipeline.write_digest(cfg, store, started).read_text()
    assert "2 new job(s) this run · 1 scored 70+" in text
    assert f"## 85 · {high['title']} at Acme" in text and low["title"] not in text
    assert f"`jobhelper prep {high['id']}`" in text


def test_cli_list_show_mark_prep(project, monkeypatch):
    store = JobStore(project / "data" / "jobs.db")
    add_jobs(store, 1)
    [row] = store.to_score(limit=1)
    store.save_score(row["id"], 88, ASSESSMENT)
    runner = CliRunner()

    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 0 and "Backend Engineer 0" in result.output and "88" in result.output

    result = runner.invoke(cli.app, ["show", str(row["id"])])
    assert result.exit_code == 0 and "Django REST APIs" in result.output

    result = runner.invoke(cli.app, ["show", "999"])
    assert result.exit_code == 1 and "No job #999" in result.output

    result = runner.invoke(cli.app, ["mark", str(row["id"]), "applied", "--note", "via referral"])
    assert result.exit_code == 0 and "new → applied" in result.output

    fake = FakeClaude(lambda body: httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                                  content=sse("## Snapshot\n- Strong fit\n\n## Cover letter\nHello")))
    monkeypatch.setattr(llm, "make_client", lambda: fake.client)
    result = runner.invoke(cli.app, ["prep", str(row["id"])])
    assert result.exit_code == 0, result.output
    [packet] = (project / "output" / "prep").glob(f"{row['id']}-acme-*.md")
    assert "## Cover letter" in packet.read_text()
    assert "Strong fit" in result.output and "Cover letter" not in result.output  # terminal shows the snapshot
    [(_, body)] = fake.requests
    assert "<screening_notes>" in body["messages"][0]["content"]


def test_cli_explains_missing_api_key(project, monkeypatch):
    monkeypatch.setattr(llm.Path, "home", lambda: project)
    store = JobStore(project / "data" / "jobs.db")
    add_jobs(store, 1)
    monkeypatch.setattr("sys.argv", ["jobhelper", "rank"])
    try:
        cli.main()
    except SystemExit as exit:
        assert exit.code == 1
    else:
        raise AssertionError("expected SystemExit")


def test_cli_resume_general_and_tailored(project, monkeypatch):
    store = JobStore(project / "data" / "jobs.db")
    add_jobs(store, 1)
    [row] = store.to_score(limit=1)
    text = "# Alex Doe\n- Built APIs [add: requests per day]\n\n## What changed\n- Stronger verbs"
    fake = FakeClaude(lambda body: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(text)))
    monkeypatch.setattr(llm, "make_client", lambda: fake.client)
    runner = CliRunner()

    result = runner.invoke(cli.app, ["resume", "--no-sync"])
    assert result.exit_code == 0, result.output
    assert "Stronger verbs" in result.output and "1 [add: …] placeholder" in result.output
    assert list((project / "output" / "resume").glob("general-*.md"))

    result = runner.invoke(cli.app, ["resume", str(row["id"]), "--no-sync"])
    assert result.exit_code == 0, result.output
    assert (project / "output" / "resume" / f"{row['id']}-acme-backend-engineer-0.md").is_file()
    assert "<job_posting>" in fake.requests[1][1]["messages"][0]["content"]
    assert fake.requests[0][1]["system"][0]["text"].startswith(llm.RESUME_INSTRUCTIONS)

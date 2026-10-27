import json

import anthropic
import httpx2
import pytest

from jobhelper.config import load_config
from jobhelper.db import JobStore, now
from jobhelper.jobs import Job

RESUME = """\
Alex Doe
Brooklyn, NY | alex@example.com
TECHNICAL SKILLS
Languages: Python, JavaScript/TypeScript, SQL
Technologies: Django, GraphQL, REST, AWS (Lambda, S3), Kubernetes
WORK EXPERIENCE
Software Engineer - Backend | Example Corp | 2021 - 2023
* Built REST APIs serving 1M requests a day.
"""

BEHAVIORAL = """\
1. Why do you want to work here?
I like building reliable APIs.

2. Tell me about a time you were wrong.
"""

CONFIG = """\
[profile]
preferences = "Backend roles in NYC."

[filters]
title_exclude = ["senior", "staff"]
locations = ["new york", "nyc"]
remote_exclude = ["canada"]

[[loops]]
name = "backend"
search = "backend engineer"

[llm]
concurrency = 2
"""

README = """\
# job-search-helper

## resume
https://docs.google.com/document/d/RESUMEDOC_aaaaaaaaaaaaaaaa/edit?usp=sharing

## behavioral
https://docs.google.com/document/d/BEHAVIORALDOC_bbbbbbbbbbbbb/edit
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project directory with config, README and already-synced documents."""
    (tmp_path / "config.toml").write_text(CONFIG)
    (tmp_path / "README.md").write_text(README)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "resume.txt").write_text(RESUME)
    (tmp_path / "data" / "behavioral.txt").write_text(BEHAVIORAL)
    monkeypatch.setenv("JOBHELPER_HOME", str(tmp_path))
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "LOOPCV_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def cfg(project):
    return load_config(project)


@pytest.fixture
def store(project):
    return JobStore(project / "data" / "jobs.db")


def make_job(**overrides) -> Job:
    fields = dict(
        source="greenhouse:acme",
        title="Software Engineer, Backend",
        company="Acme",
        location="New York, NY",
        url="https://boards.greenhouse.io/acme/jobs/1",
        description="Build Python APIs with Django and AWS.",
    )
    return Job(**{**fields, **overrides})


def add_jobs(store: JobStore, count: int) -> None:
    for i in range(count):
        store.upsert(make_job(title=f"Backend Engineer {i}", url=f"https://example.com/{i}"), keyword_hits=i, seen_at=now())
    store.commit()


def message(content, stop_reason="end_turn", model="claude-opus-5-5"):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 200, "output_tokens": 50, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3000},
    }


def sse(text: str, stop_reason: str = "end_turn") -> bytes:
    start = message([], stop_reason=None)
    start["usage"] = {"input_tokens": 200, "output_tokens": 1}
    events = [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 40}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


ASSESSMENT = {
    "score": 81,
    "summary": "Backend Python role that matches the API work.",
    "strengths": ["Django REST APIs"],
    "gaps": ["No Go"],
    "dealbreakers": [],
    "years_required": 3,
}


class FakeClaude:
    """An Anthropic client whose HTTP calls are answered locally; records what was sent."""

    def __init__(self, respond=None):
        self.requests: list[tuple[httpx2.Request, dict]] = []
        self.respond = respond or (lambda body: httpx2.Response(200, json=message([{"type": "text", "text": json.dumps(ASSESSMENT)}])))
        self.client = anthropic.Anthropic(
            api_key="test-key", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(self._handle))
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append((request, body))
        return self.respond(body)


@pytest.fixture
def fake_claude():
    return FakeClaude()

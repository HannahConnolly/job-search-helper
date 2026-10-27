import httpx2
import pytest

from jobhelper import profile
from jobhelper.profile import (
    ProfileError,
    clean_text,
    readme_doc_links,
    resume_keywords,
)

from .conftest import README, RESUME


def test_readme_links_are_matched_to_their_headings():
    assert readme_doc_links(README) == [
        ("resume", "RESUMEDOC_aaaaaaaaaaaaaaaa"),
        ("behavioral", "BEHAVIORALDOC_bbbbbbbbbbbbb"),
    ]


def test_resume_keywords_split_lists_and_groups():
    assert resume_keywords(RESUME) == [
        "Python", "JavaScript", "TypeScript", "SQL",
        "Django", "GraphQL", "REST", "AWS", "Lambda", "S3", "Kubernetes",
    ]


def test_clean_text_strips_bom_crlf_and_blank_runs():
    assert clean_text("﻿Title\r\n\r\n\r\n\r\nBody  \r\n") == "Title\n\nBody\n"


def test_load_adds_extra_keywords_without_duplicates(cfg):
    cfg.profile.extra_keywords = ["payments", "python"]
    loaded = profile.load(cfg)
    assert loaded.keywords[-1] == "payments"
    assert [k.lower() for k in loaded.keywords].count("python") == 1
    assert "<preferences>\nBackend roles in NYC.\n</preferences>" in loaded.prompt_block()


def test_sync_downloads_each_doc_from_its_readme_link(cfg, monkeypatch):
    requested = []

    def fake_get(url, **kwargs):
        requested.append(url)
        doc = "resume" if "RESUMEDOC" in url else "behavioral"
        return httpx2.Response(200, headers={"content-type": "text/plain; charset=utf-8"}, text=f"﻿{doc} text\r\n")

    monkeypatch.setattr(profile.httpx2, "get", fake_get)
    changed = profile.sync(cfg)
    assert requested == [
        "https://docs.google.com/document/d/RESUMEDOC_aaaaaaaaaaaaaaaa/export?format=txt",
        "https://docs.google.com/document/d/BEHAVIORALDOC_bbbbbbbbbbbbb/export?format=txt",
    ]
    assert changed == {"resume": True, "behavioral": True}
    assert (cfg.data_dir / "resume.txt").read_text() == "resume text\n"


def test_private_doc_gives_a_sharing_hint(cfg, monkeypatch):
    sign_in_page = httpx2.Response(200, headers={"content-type": "text/html"}, text="<html>Sign in</html>")
    monkeypatch.setattr(profile.httpx2, "get", lambda url, **kwargs: sign_in_page)
    with pytest.raises(ProfileError, match="Anyone with the link"):
        profile.sync(cfg)


def test_configured_local_file_wins_over_readme(cfg):
    (cfg.root / "my-resume.txt").write_text("Local resume\n")
    cfg.profile.resume_doc = "my-resume.txt"
    assert profile.fetch(profile._source(cfg, "resume"), cfg.root) == "Local resume\n"

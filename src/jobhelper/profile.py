"""Your resume and behavioral-question doc, synced from the Google Docs linked in README.md."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx2

from .config import Config

DOCS = ("resume", "behavioral")  # data/<name>.txt, found under a README heading containing <name>
DOC_ID = re.compile(r"docs\.google\.com/document/d/([\w-]{20,})")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
EXPORT_URL = "https://docs.google.com/document/d/{doc_id}/export?format=txt"
SKILL_LINE = re.compile(
    r"^[ \t]*(?:languages|technologies|technical skills|skills|tools|frameworks)[ \t]*:(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


class ProfileError(Exception):
    pass


@dataclass
class Profile:
    resume: str
    behavioral: str
    preferences: str
    keywords: list[str]

    def prompt_block(self) -> str:
        """Candidate context shared by every Claude request; byte-stable so it caches."""
        parts = [
            f"<resume>\n{self.resume.strip()}\n</resume>",
            f"<interview_notes>\n{self.behavioral.strip()}\n</interview_notes>",
        ]
        if self.preferences:
            parts.append(f"<preferences>\n{self.preferences}\n</preferences>")
        return "\n\n".join(parts)


def readme_doc_links(readme: str) -> list[tuple[str, str]]:
    """(heading, doc id) for each Google Doc linked in the README, in order."""
    links, heading = [], ""
    for line in readme.splitlines():
        if match := HEADING.match(line):
            heading = match.group(1).lower()
        elif match := DOC_ID.search(line):
            links.append((heading, match.group(1)))
    return links


def clean_text(text: str) -> str:
    """Google's text export has a BOM, CRLF line ends and runs of blank lines."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def resume_keywords(resume: str) -> list[str]:
    """Skills from "Languages:" / "Technologies:" lines; "AWS (Lambda, S3)" gives AWS, Lambda, S3."""
    words = []
    for match in SKILL_LINE.finditer(resume):
        words += [word.strip() for word in re.split(r"[,;/()]", match.group(1))]
    return _dedupe(word for word in words if word)


def _dedupe(words) -> list[str]:
    seen, unique = set(), []
    for word in words:
        if word.lower() not in seen:
            seen.add(word.lower())
            unique.append(word)
    return unique


def _source(cfg: Config, name: str) -> str:
    if configured := getattr(cfg.profile, f"{name}_doc"):
        return configured
    readme = cfg.root / "README.md"
    if readme.is_file():
        for heading, doc_id in readme_doc_links(readme.read_text()):
            if name in heading:
                return doc_id
    raise ProfileError(
        f"No {name} doc found: link a Google Doc under a '{name}' heading in README.md, "
        f"or set {name}_doc in the [profile] section of config.toml."
    )


def fetch(source: str, root: Path) -> str:
    """Text of a Google Doc (link or id) or of a local file."""
    if not source.startswith("http") and (path := root / source).is_file():
        return clean_text(path.read_text())
    match = DOC_ID.search(source)
    doc_id = match.group(1) if match else source
    try:
        response = httpx2.get(EXPORT_URL.format(doc_id=doc_id), follow_redirects=True, timeout=30)
    except httpx2.HTTPError as e:
        raise ProfileError(f"Couldn't download Google Doc {doc_id}: {e}") from None
    # A doc that isn't link-shared redirects to an HTML sign-in page instead of failing.
    if response.status_code != 200 or not response.headers.get("content-type", "").startswith("text/plain"):
        raise ProfileError(
            f"Couldn't export Google Doc {doc_id} (HTTP {response.status_code}). "
            "Set its sharing to 'Anyone with the link can view', or save it as a text file "
            "and point config.toml at that file."
        )
    return clean_text(response.text)


def sync(cfg: Config) -> dict[str, bool]:
    """Download both documents into data/; returns which ones changed."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    changed = {}
    for name in DOCS:
        text = fetch(_source(cfg, name), cfg.root)
        path = cfg.data_dir / f"{name}.txt"
        changed[name] = not path.is_file() or path.read_text() != text
        path.write_text(text)
    return changed


def load(cfg: Config) -> Profile:
    paths = {name: cfg.data_dir / f"{name}.txt" for name in DOCS}
    if not all(path.is_file() for path in paths.values()):
        sync(cfg)
    resume, behavioral = (paths[name].read_text() for name in DOCS)
    return Profile(
        resume=resume,
        behavioral=behavioral,
        preferences=cfg.profile.preferences.strip(),
        keywords=_dedupe(resume_keywords(resume) + cfg.profile.extra_keywords),
    )

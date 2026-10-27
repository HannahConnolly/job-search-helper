"""Cheap, deterministic checks that run before a job is stored or sent to Claude."""

from __future__ import annotations

import re

from .config import FilterConfig
from .jobs import Job

US = re.compile(r"united states(?: of america)?|usa|us|u\.s\.(?:a\.)?", re.IGNORECASE)
REMOTE = re.compile(r"(?<!\w)remote(?!\w)", re.IGNORECASE)


def phrase_pattern(phrases, ignore_case: bool = True) -> re.Pattern | None:
    """Match any of the phrases as whole words."""
    phrases = sorted({p.strip() for p in phrases if p.strip()}, key=len, reverse=True)
    if not phrases:
        return None
    alternation = "|".join(re.escape(p) for p in phrases)
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE if ignore_case else 0)


class JobFilter:
    def __init__(self, cfg: FilterConfig):
        self._include = phrase_pattern(cfg.title_include)
        self._exclude = phrase_pattern(cfg.title_exclude)
        self._local = phrase_pattern(cfg.locations)
        self._abroad = phrase_pattern(cfg.remote_exclude)
        self._allow_remote = cfg.allow_remote
        self._companies = {c.strip().lower() for c in cfg.exclude_companies}

    def reject_reason(self, job: Job, check_location: bool = True) -> str | None:
        if job.company.strip().lower() in self._companies:
            return "company"
        if self._include and not self._include.search(job.title):
            return "title"
        if self._exclude and self._exclude.search(job.title):
            return "title"
        if check_location and not self.location_ok(job):
            return "location"
        return None

    def location_ok(self, job: Job) -> bool:
        where = job.location.strip()
        if not where or (self._local and self._local.search(where)):
            return True
        if not self._allow_remote:
            return False
        if not (job.is_remote or REMOTE.search(where) or US.fullmatch(where)):
            return False
        if job.country and not US.fullmatch(job.country.strip()):
            return False
        return not (self._abroad and self._abroad.search(where))


class KeywordScorer:
    """Counts the resume skills a posting mentions; decides which jobs Claude scores first."""

    def __init__(self, keywords):
        # Short all-caps skills (REST, SQL, AWS) match case-sensitively so "rest" doesn't count.
        self._patterns = [
            phrase_pattern([k], ignore_case=not (k.isupper() and len(k) <= 5)) for k in keywords if k.strip()
        ]

    def hits(self, text: str) -> int:
        return sum(1 for pattern in self._patterns if pattern.search(text))

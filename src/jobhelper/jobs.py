"""The Job record every source produces, plus normalization helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from markdownify import markdownify

COMPANY_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "the"}
INTERVALS = {
    "yearly": "yr", "per-year-salary": "yr",
    "monthly": "mo", "per-month-salary": "mo",
    "weekly": "wk", "per-week-salary": "wk",
    "daily": "day", "per-day-wage": "day",
    "hourly": "hr", "per-hour-wage": "hr",
}


@dataclass
class Job:
    source: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    # Raw HTML, converted into `description` only for jobs that pass the filters (conversion is slow).
    description_html: str = ""
    apply_url: str | None = None
    is_remote: bool | None = None
    country: str | None = None  # used for filtering only, when a source states it
    salary: str | None = None
    date_posted: str | None = None
    loop: str | None = None

    @property
    def fingerprint(self) -> str:
        """Same company and title on different boards count as one job."""
        if not self.company:
            return self.url
        company = " ".join(w for w in _words(self.company) if w not in COMPANY_SUFFIXES)
        return f"{company}|{' '.join(_words(self.title))}"


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9+#]+", " ", text.lower()).split()


def html_to_text(html: str) -> str:
    text = markdownify(html, heading_style="ATX", bullets="-", strip=["img"])
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def format_salary(low, high, interval: str | None = None, currency: str | None = None) -> str | None:
    if not low and not high:
        return None
    symbol = "$" if currency in (None, "", "USD") else f"{currency} "
    amount = "–".join(f"{symbol}{value:,.0f}" for value in (low, high) if value)
    unit = INTERVALS.get((interval or "").lower())
    return f"{amount}/{unit}" if unit else amount

"""Terminal rendering shared by the CLI and the pipeline."""

from __future__ import annotations

import re
from datetime import datetime
from sqlite3 import Row

from rich.markup import escape
from rich.table import Table
from rich.text import Text


def fit_text(score: int | None) -> str:
    if score is None:
        return "[dim]–[/]"
    style = "bold green" if score >= 85 else "green" if score >= 70 else "yellow" if score >= 50 else "red"
    return f"[{style}]{score}[/]"


def local_date(timestamp: str) -> str:
    return datetime.fromisoformat(timestamp).astimezone().date().isoformat()


def short_location(location: str, prefer: re.Pattern | None = None) -> str:
    """"San Francisco, CA; New York, NY; Seattle, WA" -> "New York, NY +2" when New York is preferred."""
    places = [place.strip() for place in location.split(";") if place.strip()]
    if not places:
        return ""
    if prefer:
        places.sort(key=lambda place: not prefer.search(place))
    return places[0] + (f" +{len(places) - 1}" if len(places) > 1 else "")


def jobs_table(rows: list[Row], title: str, prefer: re.Pattern | None = None) -> Table | Text:
    if not rows:
        return Text(f"{title}: nothing to show.", style="dim")
    table = Table(title=title, title_justify="left", header_style="bold")
    table.add_column("ID", justify="right", style="dim", no_wrap=True)
    table.add_column("Fit", justify="right", no_wrap=True)
    table.add_column("Title", min_width=16, ratio=1)
    table.add_column("Company", max_width=14, no_wrap=True, overflow="ellipsis")
    table.add_column("Location", max_width=18, no_wrap=True, overflow="ellipsis")
    table.add_column("Status", no_wrap=True)
    for row in rows:
        table.add_row(
            str(row["id"]),
            fit_text(row["score"]),
            escape(row["title"]),
            escape(row["company"]),
            escape(short_location(row["location"], prefer)),
            row["status"],
        )
    return table

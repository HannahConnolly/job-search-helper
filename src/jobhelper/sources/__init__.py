"""Where jobs come from: JobSpy loops (Indeed, LinkedIn, ...), company job boards, and optionally LoopCV."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..jobs import Job


@dataclass
class SourceResult:
    name: str
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None
    # The source already searched by location, so the location filter is skipped for its jobs.
    location_targeted: bool = False

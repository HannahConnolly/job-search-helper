"""SQLite store for every job the loops find, Claude's assessment, and your application status."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .jobs import Job


class Status(StrEnum):
    new = "new"
    saved = "saved"
    applied = "applied"
    interviewing = "interviewing"
    offer = "offer"
    rejected = "rejected"
    skipped = "skipped"


ACTIVE = ("new", "saved")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY,
    fingerprint  TEXT NOT NULL UNIQUE,
    source       TEXT NOT NULL,
    loop         TEXT,
    title        TEXT NOT NULL,
    company      TEXT NOT NULL,
    location     TEXT NOT NULL,
    is_remote    INTEGER,
    url          TEXT NOT NULL,
    apply_url    TEXT,
    salary       TEXT,
    date_posted  TEXT,
    description  TEXT NOT NULL,
    keyword_hits INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    score        INTEGER,
    assessment   TEXT,
    scored_at    TEXT,
    status       TEXT NOT NULL DEFAULT 'new',
    status_at    TEXT,
    notes        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_score ON jobs (score);
CREATE TABLE IF NOT EXISTS loopcv_loops (
    name    TEXT PRIMARY KEY,
    loop_id TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def commit(self) -> None:
        self.conn.commit()

    def upsert(self, job: Job, keyword_hits: int, seen_at: str) -> bool:
        """Store a newly found job (True), or refresh one already stored (False)."""
        existing = self.conn.execute("SELECT id FROM jobs WHERE fingerprint = ?", (job.fingerprint,)).fetchone()
        if existing:
            self.conn.execute(
                """UPDATE jobs SET
                       last_seen = ?,
                       apply_url = COALESCE(apply_url, ?),
                       salary = COALESCE(salary, ?),
                       description = CASE WHEN length(?) > length(description) THEN ? ELSE description END,
                       keyword_hits = MAX(keyword_hits, ?)
                   WHERE id = ?""",
                (seen_at, job.apply_url, job.salary, job.description, job.description, keyword_hits, existing["id"]),
            )
            return False
        self.conn.execute(
            """INSERT INTO jobs (fingerprint, source, loop, title, company, location, is_remote, url, apply_url,
                                 salary, date_posted, description, keyword_hits, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job.fingerprint, job.source, job.loop, job.title, job.company, job.location, job.is_remote, job.url,
             job.apply_url, job.salary, job.date_posted, job.description, keyword_hits, seen_at, seen_at),
        )
        return True

    def to_score(self, limit: int, rescore: bool = False) -> list[sqlite3.Row]:
        """Jobs Claude should score next: most resume keywords first, then newest."""
        unscored = "" if rescore else "AND scored_at IS NULL"
        return self.conn.execute(
            f"""SELECT * FROM jobs WHERE status IN ('new', 'saved') {unscored}
                ORDER BY keyword_hits DESC, first_seen DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def count_unscored(self) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM jobs WHERE scored_at IS NULL AND status IN ('new', 'saved')"
        ).fetchone()[0]

    def save_score(self, job_id: int, score: int, assessment: dict) -> None:
        self.conn.execute(
            "UPDATE jobs SET score = ?, assessment = ?, scored_at = ? WHERE id = ?",
            (score, json.dumps(assessment), now(), job_id),
        )
        self.conn.commit()  # per job, so an interrupted run keeps what it paid for

    def save_failure(self, job_id: int, error: str) -> None:
        """Mark a job Claude couldn't score so it isn't retried (and paid for) every run."""
        self.conn.execute(
            "UPDATE jobs SET score = NULL, assessment = ?, scored_at = ? WHERE id = ?",
            (json.dumps({"error": error}), now(), job_id),
        )
        self.conn.commit()

    def listing(self, statuses: list[str] | None, min_score: int | None, limit: int) -> list[sqlite3.Row]:
        """Best first. min_score=None also includes unscored jobs (ranked by keyword hits)."""
        clauses, params = [], []
        if statuses:
            clauses.append(f"status IN ({', '.join('?' * len(statuses))})")
            params += statuses
        if min_score is not None:
            clauses.append("score >= ?")
            params.append(min_score)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.conn.execute(
            f"""SELECT * FROM jobs {where}
                ORDER BY score IS NULL, score DESC, keyword_hits DESC, first_seen DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()

    def found_since(self, since: str) -> int:
        return self.conn.execute("SELECT count(*) FROM jobs WHERE first_seen >= ?", (since,)).fetchone()[0]

    def scored_since(self, since: str, min_score: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE first_seen >= ? AND score >= ? ORDER BY score DESC",
            (since, min_score),
        ).fetchall()

    def get(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def set_status(self, job_id: int, status: str, note: str = "") -> None:
        stamp = now()
        if note:
            day = datetime.fromisoformat(stamp).astimezone().date().isoformat()
            self.conn.execute(
                "UPDATE jobs SET notes = notes || ? WHERE id = ?", (f"{day} ({status}): {note}\n", job_id)
            )
        self.conn.execute("UPDATE jobs SET status = ?, status_at = ? WHERE id = ?", (status, stamp, job_id))
        self.conn.commit()

    def status_counts(self) -> dict[str, int]:
        counts = dict(self.conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall())
        return {status.value: counts.get(status.value, 0) for status in Status}

    def loopcv_id(self, name: str) -> str | None:
        row = self.conn.execute("SELECT loop_id FROM loopcv_loops WHERE name = ?", (name,)).fetchone()
        return row["loop_id"] if row else None

    def set_loopcv_id(self, name: str, loop_id: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO loopcv_loops (name, loop_id) VALUES (?, ?)", (name, loop_id))
        self.conn.commit()

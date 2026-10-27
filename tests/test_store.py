from jobhelper.db import now

from .conftest import add_jobs, make_job


def test_same_job_from_two_boards_is_stored_once(store):
    seen = now()
    assert store.upsert(make_job(source="linkedin", company="Acme, Inc.", description="short"), 2, seen)
    duplicate = make_job(source="greenhouse:acme", company="ACME", apply_url="https://acme/apply",
                         description="a much longer description")
    assert not store.upsert(duplicate, 5, seen)
    [row] = store.listing(None, None, 10)
    assert row["source"] == "linkedin"
    assert row["apply_url"] == "https://acme/apply"
    assert row["description"] == "a much longer description"
    assert row["keyword_hits"] == 5


def test_scoring_queue_prefers_keyword_matches_and_skips_scored(store):
    add_jobs(store, 3)
    queue = store.to_score(limit=10)
    assert [row["keyword_hits"] for row in queue] == [2, 1, 0]
    store.save_score(queue[0]["id"], 90, {"summary": "x"})
    assert len(store.to_score(limit=10)) == 2
    assert len(store.to_score(limit=10, rescore=True)) == 3


def test_listing_cutoff_and_status(store):
    add_jobs(store, 3)
    first, second, _ = store.to_score(limit=10)
    store.save_score(first["id"], 88, {})
    store.save_score(second["id"], 60, {})
    assert [row["score"] for row in store.listing(["new", "saved"], 70, 10)] == [88]
    assert [row["score"] for row in store.listing(None, None, 10)] == [88, 60, None]

    store.set_status(first["id"], "applied", "Referred by a friend")
    row = store.get(first["id"])
    assert row["status"] == "applied" and "(applied): Referred by a friend" in row["notes"]
    assert store.status_counts()["applied"] == 1
    assert store.listing(["new", "saved"], 70, 10) == []

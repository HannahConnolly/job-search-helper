import pytest

from jobhelper.config import FilterConfig
from jobhelper.filters import JobFilter, KeywordScorer

from .conftest import make_job

FILTER = JobFilter(FilterConfig(
    title_include=["engineer", "developer"],
    title_exclude=["senior", "sr", "staff", "front-end", "security engineer"],
    locations=["new york", "nyc"],
    remote_exclude=["canada", "uk"],
    exclude_companies=["Bad Co"],
))


@pytest.mark.parametrize("title, kept", [
    ("Software Engineer, Backend", True),
    ("Backend Developer II", True),
    ("Senior Software Engineer", False),
    ("Sr. Backend Engineer", False),
    ("Staff Engineer", False),
    ("Front-End Engineer", False),
    ("Security Engineer", False),
    ("Software Engineer, Security", True),  # whole phrases only
    ("Product Manager", False),             # no include word
    ("Engineering Manager", False),         # "engineering" isn't "engineer"
])
def test_titles(title, kept):
    assert (FILTER.reject_reason(make_job(title=title)) is None) == kept


@pytest.mark.parametrize("location, is_remote, country, kept", [
    ("New York, NY", None, None, True),
    ("San Francisco, CA; New York, NY", None, None, True),
    ("San Francisco, CA", None, None, False),
    ("Remote - US", None, None, True),
    ("United States", None, None, True),
    ("Remote - Canada", None, None, False),
    ("Belfast, Northern Ireland, Remote", True, "GB", False),  # Lever gives a country code
    ("Remote", True, "United States", True),
    ("", None, None, True),  # unknown: let Claude judge
])
def test_locations(location, is_remote, country, kept):
    job = make_job(location=location, is_remote=is_remote, country=country)
    assert FILTER.location_ok(job) == kept


def test_location_check_can_be_skipped_for_targeted_searches():
    assert FILTER.reject_reason(make_job(location="Stamford, CT"), check_location=False) is None


def test_excluded_company():
    assert FILTER.reject_reason(make_job(company="bad co")) == "company"


def test_keyword_hits_treat_short_acronyms_case_sensitively():
    scorer = KeywordScorer(["REST", "Python", "Java", "AWS"])
    assert scorer.hits("Get some rest. We use JavaScript.") == 0
    assert scorer.hits("REST APIs in python on AWS; some Java") == 4

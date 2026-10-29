"""The hosted board scores jobs in the browser with match.js, a port of matching.py. Run both on the
same jobs and settings and check they agree."""

from __future__ import annotations

import itertools
import json
import random
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jobbot.matching import heuristic_score, hidden_reason, is_relevant, relevance_terms
from jobbot.models import Job
from jobbot.publish import feed_job
from jobbot.schemas import Filters, Profile
from jobbot.text import detect_seniority, extract_years_required, job_fingerprint

MATCH_JS = Path(__file__).parents[1] / "src" / "jobbot" / "board" / "match.js"
NOW = datetime(2026, 10, 26, 12, 0, 0)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js isn't installed")

HARNESS = f"""
import * as m from {json.dumps(MATCH_JS.as_uri())};
let input = "";
for await (const chunk of process.stdin) input += chunk;
const {{ now, cases }} = JSON.parse(input);
const out = cases.map(({{ doc, job }}) => {{
  const terms = m.relevanceTerms(doc);
  const e = m.estimate(job, doc, terms, now);
  return {{ relevant: m.isRelevant(job.title, terms), hidden: m.hiddenReason(job, doc, now),
           score: e.score, breakdown: e.breakdown, skills: e.skills, notes: e.notes }};
}});
process.stdout.write(JSON.stringify(out));
"""

TITLES = [
    "Senior Backend Engineer",
    "Backend Engineer (Remote)",
    "Sr. Data Engineer",
    "Machine Learning Engineer II",
    "Front-End Developer",
    "Account Executive",
    "Staff Software Engineer, AI",
    "Software Engineer - New Grad",
    "AI/ML Engineer",
    "Data Scientist & Analyst",
    "Back end Engineer, Payments",
    "Engineering Intern",
]
LOCATIONS = ["New York, NY", "Remote - US", "Austin, TX", "London, UK", "", "Sunnyvale, CA"]
DESCRIPTIONS = [
    "Build APIs in Python and SQL. 3+ years of experience required.",
    "We use C++ and C#; Node.js is a plus. Minimum of 5 years.",
    "Must be a U.S. citizen. Active secret clearance required. Spark, Kafka.",
    "We are unable to sponsor visas. React, TypeScript, 2-4 years.",
    "Unpaid internship. Ten years old company. Python.",
    "Python, SQL and Kafka services, with some C++ and Node.js. 1+ years of experience.",
    "",
]
SALARIES = [
    (None, None, None, None),
    (90000, 120000, "USD", "year"),
    (60, 80, "USD", "hour"),
    (150000, None, "EUR", "year"),
]


def make_job(i: int, rng: random.Random) -> Job:
    title = TITLES[i % len(TITLES)]
    location = rng.choice(LOCATIONS)
    description = rng.choice(DESCRIPTIONS)
    low, high, currency, period = rng.choice(SALARIES)
    company = rng.choice(["Acme Inc.", "The Globex Corporation", "Initech LLC", "Umbrella"])
    return Job(
        fingerprint=job_fingerprint(company, title, location) + str(i),
        source="greenhouse",
        url=f"https://example.com/{i}",
        title=title,
        company=company,
        location=location,
        workplace=rng.choice([None, "remote", "hybrid", "onsite"]),
        salary_min=low,
        salary_max=high,
        salary_currency=currency,
        salary_period=period,
        description=description,
        posted_at=rng.choice([None, NOW - timedelta(days=rng.choice([0.5, 2, 5, 10, 20, 40, 90]))]),
        first_seen_at=NOW,
        seniority=detect_seniority(title),
        years_required=extract_years_required(description),
        sources=[],
    )


PROFILES = [
    Profile(),
    Profile(
        years_experience=3,
        target_titles=["Backend Engineer", "Data Engineer"],
        skills=["Python", "SQL", "C++", "Node.js", "Kafka"],
        workplace_preferences=["remote", "hybrid"],
    ),
    Profile(
        years_experience=0,
        target_titles=["AI Engineer", "Machine Learning Engineer", "Software Engineer"],
        skills=["Python", "PyTorch", "React"],
        requires_sponsorship=True,
    ),
    Profile(
        years_experience=8.5, target_titles=["Staff Engineer"], security_clearance="Secret", skills=["C#"]
    ),
]
FILTERS = [
    Filters(),
    Filters(
        title_exclude=["senior", "intern"],
        locations=["New York", "TX"],
        include_remote=False,
        min_salary=100000,
        max_years_gap=None,
        max_age_days=None,
    ),
    Filters(
        title_include=["developer"],
        company_exclude=["Globex"],
        description_exclude=["unpaid"],
        location_exclude=["London"],
        locations=["NY"],
        max_years_gap=0,
        max_age_days=7,
        hide_no_sponsorship=True,
    ),
]


def test_browser_scoring_matches_python():
    rng = random.Random(7)
    jobs = [make_job(i, rng) for i in range(48)]
    cases, expected = [], []
    for profile, filters, job in itertools.product(PROFILES, FILTERS, jobs):
        terms = relevance_terms(filters, profile, [])
        score, breakdown = heuristic_score(job, profile, terms, now=NOW)
        expected.append(
            {
                "relevant": is_relevant(job.title, terms),
                "hidden": hidden_reason(job, profile, filters, now=NOW),
                "score": score,
                "breakdown": {
                    k: breakdown[k] for k in ("title", "skills", "experience", "freshness", "workplace")
                },
                "skills": breakdown["matched_skills"],
                "notes": breakdown["notes"],
            }
        )
        doc = {
            "profile": profile.model_dump(mode="json"),
            "settings": {"filters": filters.model_dump(mode="json")},
        }
        cases.append({"doc": doc, "job": feed_job(job)})

    now_ms = int((NOW - datetime(1970, 1, 1)).total_seconds() * 1000)
    result = subprocess.run(
        ["node", "--input-type=module", "-e", HARNESS],
        input=json.dumps({"now": now_ms, "cases": cases}),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    actual = json.loads(result.stdout)
    assert len(actual) == len(expected)
    mismatches = [
        (case["job"]["title"], case["doc"]["profile"]["target_titles"], want, got)
        for case, want, got in zip(cases, expected, actual, strict=True)
        if want != got
    ]
    assert not mismatches, mismatches[:3]
    # The cases cover what matters: hidden and shown jobs, high and low scores.
    assert {e["hidden"] is None for e in expected} == {True, False}
    assert len({e["hidden"].split(" ")[0] for e in expected if e["hidden"]}) >= 6
    assert max(e["score"] for e in expected) >= 75 and min(e["score"] for e in expected) <= 30

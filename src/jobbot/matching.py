"""Relevance gate, hard filters and the heuristic fit score.

The heuristic score (0-100) works without any API key. When Claude is configured, the AI fit
analysis replaces it for the jobs it reviews.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import datetime

from jobbot.models import Job
from jobbot.schemas import WORKPLACES, Filters, Profile
from jobbot.text import (
    age_days,
    annualize,
    format_salary,
    keyword_in_text,
    normalize_company,
    normalize_title,
    term_matches,
    title_tokens,
)


def relevance_terms(filters: Filters, profile: Profile, search_keywords: Iterable[str]) -> list[str]:
    """Title terms that make a job from a company board worth keeping."""
    seen: set[str] = set()
    terms = []
    for term in [*filters.title_include, *profile.target_titles, *search_keywords]:
        key = " ".join(title_tokens(term))
        if key and key not in seen:
            seen.add(key)
            terms.append(term.strip())
    return terms


def is_relevant(title: str, terms: list[str]) -> bool:
    return not terms or any(term_matches(term, title) for term in terms)


_NO_SPONSORSHIP = re.compile(
    r"(?:unable|not able|cannot|can ?not|can't|will not|won't|do not|does not|don't|doesn't|are not|is not)"
    r"\s+(?:able\s+to\s+)?(?:to\s+)?(?:offer|provide|support)?\s*(?:visa\s+|immigration\s+|employment\s+)?sponsor"
    r"|\bno\s+(?:visa\s+|immigration\s+)?sponsorship"
    r"|sponsorship\s+(?:is\s+)?(?:not|un)\s*(?:available|offered|provided|possible)"
    r"|without\s+(?:the\s+need\s+for\s+|requiring\s+|needing\s+)?(?:current\s+or\s+future\s+|future\s+)?"
    r"(?:visa\s+|employer\s+|employment\s+|immigration\s+)?sponsorship"
    r"|must\s+be\s+(?:a\s+)?(?:u\.?s\.?|united states)\s+citizen"
    r"|(?:u\.?s\.?|united states)\s+citizenship\s+(?:is\s+)?required",
    re.I,
)
_CLEARANCE = re.compile(
    r"(?:active|current)\s+(?:dod\s+)?(?:secret|top[\s-]secret|ts(?:/sci)?|security|public trust)\s+clearance"
    r"|(?:secret|top[\s-]secret|ts/sci|security)\s+clearance\s+(?:is\s+)?required"
    r"|must\s+(?:have|hold|possess|maintain)\s+(?:an?\s+)?(?:active\s+)?(?:dod\s+)?"
    r"(?:secret|top[\s-]secret|ts(?:/sci)?|security)\s+clearance"
    r"|\bts/sci\s+(?:with\s+(?:full[\s-]scope\s+)?poly(?:graph)?\s+)?(?:is\s+)?required",
    re.I,
)


def mentions_no_sponsorship(text: str) -> bool:
    return bool(_NO_SPONSORSHIP.search(text or ""))


def requires_clearance(text: str) -> bool:
    return bool(_CLEARANCE.search(text or ""))


def _years(value: float) -> str:
    return f"{value:g}"


def hidden_reason(job: Job, profile: Profile, filters: Filters, now: datetime | None = None) -> str | None:
    """Why this job fails one of your hard filters, or None if it passes them all."""
    title = job.title or ""
    location = job.location or ""
    description = job.description or ""

    for keyword in filters.title_exclude:
        if keyword.strip() and (term_matches(keyword, title) or keyword_in_text(keyword, title)):
            return f"Title contains “{keyword.strip()}”"

    company = normalize_company(job.company or "")
    for excluded in filters.company_exclude:
        if excluded.strip() and normalize_company(excluded) == company:
            return f"Company is on your exclude list ({job.company})"

    for keyword in filters.description_exclude:
        if keyword.strip() and keyword_in_text(keyword, description):
            return f"Description mentions “{keyword.strip()}”"

    # Whole words, so "NY" doesn't match "Sunnyvale" and "CA" doesn't match "Chicago".
    for excluded in filters.location_exclude:
        if excluded.strip() and keyword_in_text(excluded, location):
            return f"Location excluded ({location})"
    if job.workplace == "remote" and not filters.include_remote:
        return "Remote roles are turned off in your filters"
    if filters.locations and location:
        matches_location = any(p.strip() and keyword_in_text(p, location) for p in filters.locations)
        if not matches_location and not (job.workplace == "remote" and filters.include_remote):
            return f"Location not in your list ({location})"

    if (
        filters.max_years_gap is not None
        and job.years_required
        and profile.years_experience is not None
        and job.years_required > profile.years_experience + filters.max_years_gap
    ):
        return f"Asks for {job.years_required}+ years (you have {_years(profile.years_experience)})"

    if filters.min_salary and (job.salary_max or job.salary_min):
        same_currency = not job.salary_currency or job.salary_currency == profile.salary_currency
        top = annualize(job.salary_max or job.salary_min, job.salary_period)
        if same_currency and top is not None and top < filters.min_salary:
            pay = format_salary(job.salary_min, job.salary_max, job.salary_currency, job.salary_period)
            return f"Pay ({pay}) is below your minimum"

    if profile.requires_sponsorship and filters.hide_no_sponsorship and mentions_no_sponsorship(description):
        return "Says it can't sponsor work visas"

    if not profile.security_clearance and filters.hide_clearance_required and requires_clearance(description):
        return "Requires an active security clearance"

    if filters.max_age_days and job.posted_at:
        age = age_days(job.posted_at, now)
        if age is not None and age > filters.max_age_days:
            return f"Posted {int(age)} days ago"

    return None


_LEVEL_MIN_YEARS = {
    "intern": 0, "entry": 0, "mid": 2, "senior": 5, "lead": 6, "staff": 8, "principal": 10, "director": 10,
}  # fmt: skip


def _title_overlap(term: str, title: str) -> float:
    words = set(title_tokens(term))
    if not words:
        return 0.0
    return len(words & set(title_tokens(title))) / len(words)


def heuristic_score(
    job: Job, profile: Profile, terms: list[str], now: datetime | None = None
) -> tuple[int, dict]:
    """A transparent 0-100 fit estimate from title, skills, experience, freshness and workplace."""
    notes: list[str] = []
    targets = [*profile.target_titles, *terms]

    # Title (0-30)
    matched_term = next((t for t in targets if term_matches(t, job.title)), None)
    if matched_term:
        exact = normalize_title(matched_term) == normalize_title(job.title)
        title_pts = 30 if exact else 25
    elif targets:
        title_pts = round(20 * max(_title_overlap(t, job.title) for t in targets))
    else:
        title_pts = 15

    # Skills (0-35), saturating so a handful of strong matches already scores well
    haystack = f"{job.title}\n{job.description or ''}"
    matched_skills = [s for s in profile.skills if keyword_in_text(s, haystack)]
    if profile.skills:
        skills_pts = round(35 * (1 - math.exp(-len(matched_skills) / 4)))
    else:
        skills_pts = 15
        notes.append("Add skills to your profile for better matching")

    # Experience (0-20)
    years, required = profile.years_experience, job.years_required
    if required is None or years is None:
        exp_pts = 14
    else:
        gap = required - years
        exp_pts = 20 if gap <= 0 else 14 if gap <= 1 else 8 if gap <= 2 else 0
        if gap > 0:
            notes.append(f"Asks for {required}+ years")
    if years is not None and job.seniority:
        if years + 2 < _LEVEL_MIN_YEARS.get(job.seniority, 0):
            exp_pts = max(0, exp_pts - 10)
            notes.append(f"{job.seniority.title()}-level title")
        elif job.seniority in ("intern", "entry") and years >= 5:
            exp_pts = max(0, exp_pts - 8)
            notes.append("Probably too junior for you")

    # Freshness (0-10): applying in the first few days matters
    age = age_days(job.posted_at, now)
    if age is None:
        fresh_pts = 5
    else:
        fresh_pts = 10 if age <= 3 else 8 if age <= 7 else 5 if age <= 14 else 2 if age <= 30 else 0

    # Workplace preference (0-5)
    prefs = profile.workplace_preferences or WORKPLACES
    if job.workplace and job.workplace not in prefs:
        place_pts = 0
        notes.append(f"{job.workplace.title()} (not your preference)")
    else:
        place_pts = 5

    total = title_pts + skills_pts + exp_pts + fresh_pts + place_pts
    breakdown = {
        "title": title_pts,
        "skills": skills_pts,
        "experience": exp_pts,
        "freshness": fresh_pts,
        "workplace": place_pts,
        "matched_skills": matched_skills[:25],
        "notes": notes,
    }
    return max(0, min(100, total)), breakdown


def score_band(score: int | None) -> str:
    if score is None:
        return "none"
    if score >= 80:
        return "high"
    if score >= 60:
        return "medium"
    if score >= 40:
        return "low"
    return "poor"

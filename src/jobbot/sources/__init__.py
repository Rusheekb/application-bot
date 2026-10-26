"""Job sources: company career sites (ATS boards) and job-board searches."""

from __future__ import annotations

from types import ModuleType

from jobbot.sources import (
    adzuna,
    ashby,
    greenhouse,
    jobspy_source,
    jsearch,
    lever,
    recruitee,
    remoteok,
    remotive,
    smartrecruiters,
    workable,
    workday,
)
from jobbot.sources.base import CompanyRef, JobPosting, SearchRef, SourceError

COMPANY_SOURCES: dict[str, ModuleType] = {
    m.ATS: m for m in (greenhouse, lever, ashby, smartrecruiters, workable, workday, recruitee)
}
# Search sources implemented with an API client (JobSpy sites are handled separately).
API_SEARCH_SOURCES: dict[str, ModuleType] = {m.KEY: m for m in (adzuna, jsearch, remotive, remoteok)}
JOBSPY_SITES = jobspy_source.SITES

SEARCH_SOURCE_LABELS: dict[str, str] = {**JOBSPY_SITES, **{k: m.LABEL for k, m in API_SEARCH_SOURCES.items()}}
SOURCE_LABELS: dict[str, str] = {
    **{k: m.LABEL for k, m in COMPANY_SOURCES.items()},
    **SEARCH_SOURCE_LABELS,
}
DEFAULT_SEARCH_SOURCES = ["linkedin", "indeed"]

# When the same job shows up in several places, keep the richest record as the primary one.
SOURCE_PRIORITY: dict[str, int] = {**{k: 3 for k in COMPANY_SOURCES}, "jsearch": 2, "adzuna": 1}


def source_label(key: str | None) -> str:
    return SOURCE_LABELS.get(key or "", (key or "").replace("_", " ").title())


def priority(source: str) -> int:
    return SOURCE_PRIORITY.get(source, 1)


__all__ = [
    "API_SEARCH_SOURCES",
    "COMPANY_SOURCES",
    "DEFAULT_SEARCH_SOURCES",
    "JOBSPY_SITES",
    "SEARCH_SOURCE_LABELS",
    "SOURCE_LABELS",
    "CompanyRef",
    "JobPosting",
    "SearchRef",
    "SourceError",
    "priority",
    "source_label",
]

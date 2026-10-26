"""SQLAlchemy ORM models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite has no timezone support; everything is stored as UTC)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    source_job_id: Mapped[str | None] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    ats: Mapped[str | None] = mapped_column(String(32))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))

    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(200), index=True)
    location: Mapped[str | None] = mapped_column(String(300))
    workplace: Mapped[str | None] = mapped_column(String(16))
    employment_type: Mapped[str | None] = mapped_column(String(60))
    department: Mapped[str | None] = mapped_column(String(200))
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    salary_period: Mapped[str | None] = mapped_column(String(8))
    description: Mapped[str] = mapped_column(Text, default="")

    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)

    seniority: Mapped[str | None] = mapped_column(String(20))
    years_required: Mapped[int | None] = mapped_column(Integer)

    score: Mapped[int | None] = mapped_column(Integer, index=True)
    score_kind: Mapped[str | None] = mapped_column(String(10))
    heuristic: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    fit: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    fit_error: Mapped[str | None] = mapped_column(Text)
    fit_at: Mapped[datetime | None] = mapped_column(DateTime)

    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    hidden_reason: Mapped[str | None] = mapped_column(String(300))
    show_anyway: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    cover_letter: Mapped[str | None] = mapped_column(Text)
    found_by: Mapped[str | None] = mapped_column(String(300))
    sources: Mapped[list[Any]] = mapped_column(JSON, default=list)

    application_runs: Mapped[list[ApplicationRun]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="ApplicationRun.id.desc()"
    )

    @property
    def is_hidden(self) -> bool:
        return bool(self.hidden_reason) and not self.show_anyway

    @property
    def likelihood(self) -> str | None:
        return (self.fit or {}).get("interview_likelihood")


class SearchQuery(Base):
    __tablename__ = "searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keywords: Mapped[str] = mapped_column(String(300))
    location: Mapped[str] = mapped_column(String(200), default="")
    remote_only: Mapped[bool] = mapped_column(Boolean, default=False)
    sources: Mapped[list[Any]] = mapped_column(JSON, default=list)
    results_per_source: Mapped[int] = mapped_column(Integer, default=25)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(Text)

    @property
    def label(self) -> str:
        where = "Remote" if self.remote_only and not self.location else self.location
        return f"{self.keywords} · {where}" if where else self.keywords


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    ats: Mapped[str] = mapped_column(String(32))
    slug: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    careers_url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(Text)
    last_job_count: Mapped[int | None] = mapped_column(Integer)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    errors: Mapped[list[Any]] = mapped_column(JSON, default=list)
    progress: Mapped[str | None] = mapped_column(String(300))


class ApplicationRun(Base):
    __tablename__ = "application_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    parent_run_id: Mapped[int | None] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(10), default="review")
    trigger: Mapped[str] = mapped_column(String(10), default="manual")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    start_url: Mapped[str | None] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[list[Any]] = mapped_column(JSON, default=list)
    answers: Mapped[list[Any]] = mapped_column(JSON, default=list)
    missing_info: Mapped[list[Any]] = mapped_column(JSON, default=list)
    generated_answers: Mapped[list[Any]] = mapped_column(JSON, default=list)
    screenshot: Mapped[str | None] = mapped_column(String(300))
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    job: Mapped[Job] = relationship(back_populates="application_runs")

    @property
    def is_active(self) -> bool:
        return self.status in ("queued", "running")


class KeyValue(Base):
    """Singleton JSON documents: the profile, settings and resume metadata."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

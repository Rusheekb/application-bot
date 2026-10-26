"""Pydantic models for user-editable documents stored as JSON: the profile and the app settings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

JOB_STATUSES = ["new", "saved", "applied", "interviewing", "offer", "rejected", "dismissed"]
STATUS_LABELS = {
    "new": "New",
    "saved": "Saved",
    "applied": "Applied",
    "interviewing": "Interviewing",
    "offer": "Offer",
    "rejected": "Rejected",
    "dismissed": "Dismissed",
}
WORKPLACES = ["remote", "hybrid", "onsite"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Education(BaseModel):
    school: str = ""
    degree: str = ""
    field_of_study: str = ""
    graduation_year: str = ""


class QAPair(BaseModel):
    question: str
    answer: str


class Profile(BaseModel):
    """Everything the bot knows about the candidate. Empty strings mean "not provided"."""

    first_name: str = ""
    last_name: str = ""
    preferred_name: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = ""

    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    other_url: str = ""

    current_title: str = ""
    current_company: str = ""
    years_experience: float | None = None
    target_titles: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    summary: str = ""
    education: list[Education] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)

    work_authorization: str = ""
    requires_sponsorship: bool = False
    security_clearance: str = ""
    willing_to_relocate: bool = False
    workplace_preferences: list[str] = Field(default_factory=lambda: list(WORKPLACES))

    desired_salary: int | None = None
    salary_currency: str = "USD"
    salary_expectation_text: str = ""
    notice_period: str = ""
    earliest_start_date: str = ""

    # Voluntary self-identification. Blank means "decline to self-identify".
    gender: str = ""
    race_ethnicity: str = ""
    veteran_status: str = ""
    disability_status: str = ""
    pronouns: str = ""

    how_did_you_hear: str = ""
    qa: list[QAPair] = Field(default_factory=list)
    agent_instructions: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()

    @property
    def location_text(self) -> str:
        return ", ".join(p for p in (self.city, self.state, self.country) if p)

    def completeness(self) -> list[str]:
        """Names of the most important missing fields, for nudges in the UI."""
        missing = []
        if not self.full_name:
            missing.append("name")
        if not self.email:
            missing.append("email")
        if not self.target_titles:
            missing.append("target titles")
        if not self.skills:
            missing.append("skills")
        if self.years_experience is None:
            missing.append("years of experience")
        return missing


class Filters(BaseModel):
    """Hard filters. Jobs that fail one are kept but hidden, with the reason shown."""

    title_include: list[str] = Field(default_factory=list)
    title_exclude: list[str] = Field(default_factory=list)
    description_exclude: list[str] = Field(default_factory=list)
    company_exclude: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    location_exclude: list[str] = Field(default_factory=list)
    include_remote: bool = True
    min_salary: int | None = None
    max_years_gap: int | None = 2
    max_age_days: int | None = 30
    hide_no_sponsorship: bool = True
    hide_clearance_required: bool = True


class AISettings(BaseModel):
    enabled: bool = True
    model: str = "claude-opus-5-5"
    scoring_effort: Effort = "low"
    agent_effort: Effort = "medium"
    max_scores_per_scan: int = 25
    min_heuristic_score: int = 30
    # Server-side refusal fallbacks (re-run a declined request on another model).
    fallbacks: bool = True


class ApplySettings(BaseModel):
    enabled: bool = False
    # review: fill the form and stop for your approval. submit: submit without asking.
    mode: Literal["review", "submit"] = "review"
    headless: bool = True
    auto_after_scan: bool = False
    auto_min_score: int = 85
    daily_limit: int = 10
    max_steps: int = 40
    allow_consent_checkboxes: bool = True
    live_session_minutes: int = 30


class NotifySettings(BaseModel):
    webhook_url: str = ""
    min_score: int = 70


class AppSettings(BaseModel):
    scan_interval_hours: float = 6.0
    filters: Filters = Field(default_factory=Filters)
    ai: AISettings = Field(default_factory=AISettings)
    apply: ApplySettings = Field(default_factory=ApplySettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    # JobSpy options (LinkedIn/Indeed/...): Indeed country and how far back to look.
    jobspy_country: str = "usa"
    jobspy_hours_old: int = 72


class FitAnalysis(BaseModel):
    """Claude's assessment of how realistic an interview is for this posting."""

    score: int
    interview_likelihood: Literal["high", "medium", "low", "very_low"]
    summary: str
    strengths: list[str]
    gaps: list[str]
    dealbreakers: list[str]
    seniority_fit: Literal["under", "match", "over", "unclear"]
    tips: list[str]


class ResumeExtraction(BaseModel):
    """Structured data Claude pulls out of an uploaded resume to pre-fill the profile."""

    first_name: str
    last_name: str
    email: str
    phone: str
    city: str
    state: str
    country: str
    linkedin_url: str
    github_url: str
    portfolio_url: str
    current_title: str
    current_company: str
    years_experience: float | None
    skills: list[str]
    summary: str
    education: list[Education]
    certifications: list[str]
    languages: list[str]
    suggested_target_titles: list[str]

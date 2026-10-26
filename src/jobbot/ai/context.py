"""Render the candidate profile and job postings as prompt text.

Output must be deterministic (fixed field order, no timestamps) so prompt caching keeps working.
"""

from __future__ import annotations

from jobbot.models import Job
from jobbot.schemas import Profile
from jobbot.sources import source_label
from jobbot.text import format_salary

MAX_DESCRIPTION_CHARS = 30_000
MAX_RESUME_CHARS = 40_000


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _line(label: str, value: object) -> str | None:
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return f"{label}: {value}"


def _education(profile: Profile) -> str:
    items = []
    for e in profile.education:
        parts = [p for p in (e.degree, e.field_of_study) if p]
        text = " in ".join(parts) if parts else ""
        where = f"{e.school}" + (f" ({e.graduation_year})" if e.graduation_year else "")
        items.append(f"{text}, {where}".strip(", ") if text else where)
    return "; ".join(i for i in items if i)


def candidate_block(profile: Profile, resume_text: str) -> str:
    """The candidate as seen by the fit analysis (no contact details)."""
    years = f"{profile.years_experience:g}" if profile.years_experience is not None else None
    salary = None
    if profile.desired_salary:
        salary = f"{profile.desired_salary:,} {profile.salary_currency} per year"
    lines = [
        _line("Current role", " at ".join(p for p in (profile.current_title, profile.current_company) if p)),
        _line("Years of professional experience", years),
        _line("Target roles", profile.target_titles),
        _line("Skills", profile.skills),
        _line("Education", _education(profile)),
        _line("Certifications", profile.certifications),
        _line("Languages", profile.languages),
        _line("Work authorization", profile.work_authorization),
        _line("Needs visa sponsorship", _yes_no(profile.requires_sponsorship)),
        _line("Security clearance", profile.security_clearance or "none"),
        _line("Based in", profile.location_text),
        _line("Open to", [w for w in profile.workplace_preferences]),
        _line("Willing to relocate", _yes_no(profile.willing_to_relocate)),
        _line("Desired salary", salary or profile.salary_expectation_text),
        _line("Summary", profile.summary),
    ]
    body = "\n".join(line for line in lines if line)
    resume = (resume_text or "").strip()[:MAX_RESUME_CHARS] or "(no resume uploaded)"
    return f"<candidate_profile>\n{body}\n</candidate_profile>\n\n<resume>\n{resume}\n</resume>"


def application_profile_block(profile: Profile, resume_text: str) -> str:
    """Everything the apply agent may put into an application form."""
    eeo_default = "(blank: decline to self-identify)"
    lines = [
        "## Contact",
        _line("First name", profile.first_name),
        _line("Last name", profile.last_name),
        _line("Preferred name", profile.preferred_name),
        _line("Email", profile.email),
        _line("Phone", profile.phone),
        _line("Street address", profile.address),
        _line("City", profile.city),
        _line("State/region", profile.state),
        _line("Postal code", profile.postal_code),
        _line("Country", profile.country),
        _line("LinkedIn", profile.linkedin_url),
        _line("GitHub", profile.github_url),
        _line("Portfolio/website", profile.portfolio_url),
        _line("Other link", profile.other_url),
        "",
        "## Experience",
        _line("Current title", profile.current_title),
        _line("Current company", profile.current_company),
        _line(
            "Years of professional experience",
            f"{profile.years_experience:g}" if profile.years_experience is not None else None,
        ),
        _line("Skills", profile.skills),
        _line("Education", _education(profile)),
        _line("Certifications", profile.certifications),
        _line("Languages", profile.languages),
        _line("Professional summary", profile.summary),
        "",
        "## Eligibility and logistics",
        _line("Work authorization", profile.work_authorization),
        _line("Requires visa sponsorship now or in the future", _yes_no(profile.requires_sponsorship)),
        _line("Security clearance", profile.security_clearance or "none"),
        _line("Willing to relocate", _yes_no(profile.willing_to_relocate)),
        _line("Open to work arrangements", profile.workplace_preferences),
        _line(
            "Desired salary (annual)",
            f"{profile.desired_salary:,} {profile.salary_currency}" if profile.desired_salary else None,
        ),
        _line("Salary expectation wording", profile.salary_expectation_text),
        _line("Notice period", profile.notice_period),
        _line("Earliest start date", profile.earliest_start_date),
        _line("How did you hear about us (default answer)", profile.how_did_you_hear),
        "",
        "## Voluntary self-identification",
        f"Gender: {profile.gender or eeo_default}",
        f"Race/ethnicity: {profile.race_ethnicity or eeo_default}",
        f"Veteran status: {profile.veteran_status or eeo_default}",
        f"Disability status: {profile.disability_status or eeo_default}",
        f"Pronouns: {profile.pronouns or eeo_default}",
    ]
    if profile.qa:
        lines += ["", "## Saved answers to common application questions"]
        for pair in profile.qa:
            lines.append(f"Q: {pair.question.strip()}\nA: {pair.answer.strip()}")
    if profile.agent_instructions.strip():
        lines += ["", "## Candidate's instructions for applications", profile.agent_instructions.strip()]
    body = "\n".join(line for line in lines if line is not None)
    resume = (resume_text or "").strip()[:MAX_RESUME_CHARS] or "(no resume uploaded)"
    return f"<candidate>\n{body}\n</candidate>\n\n<resume>\n{resume}\n</resume>"


def job_block(job: Job) -> str:
    salary = format_salary(job.salary_min, job.salary_max, job.salary_currency, job.salary_period)
    description = (job.description or "").strip()
    note = ""
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS]
        note = "\n[Description truncated]"
    if not description:
        description = "(no description available)"
    lines = [
        _line("Title", job.title),
        _line("Company", job.company),
        _line("Location", job.location),
        _line("Workplace", job.workplace),
        _line("Employment type", job.employment_type),
        _line("Department", job.department),
        _line("Salary", salary),
        _line("Posted", job.posted_at.date().isoformat() if job.posted_at else None),
        _line("Source", source_label(job.source)),
    ]
    header = "\n".join(line for line in lines if line)
    return f"<job_posting>\n{header}\n\nDescription:\n{description}{note}\n</job_posting>"

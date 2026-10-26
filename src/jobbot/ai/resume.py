"""Resume text extraction and AI-assisted profile pre-filling."""

from __future__ import annotations

import base64
from pathlib import Path

from jobbot.ai.client import LLM
from jobbot.schemas import Education, Profile, ResumeExtraction

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
MAX_RESUME_BYTES = 10 * 1024 * 1024


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if suffix == ".docx":
        import docx

        document = docx.Document(str(path))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(p for p in parts if p.strip()).strip()
    return path.read_text(errors="replace").strip()


RESUME_SYSTEM = """Extract structured profile data from the candidate's resume.

Rules:
- Copy facts exactly as written. Use an empty string, empty list or null for anything the resume doesn't state; never guess contact details or links.
- years_experience: total years of professional experience, computed from the dates of full-time roles (count overlapping roles once; internships count as half). Null if dates are missing.
- skills: concrete skills, tools, languages and frameworks mentioned (at most 40), most prominent first.
- summary: two or three sentences describing the candidate's experience, written in the third person.
- suggested_target_titles: 3 to 6 job titles this person could realistically get interviews for in their next role, judged from their experience level. Include their current level, plus one step up only when the resume clearly supports it."""


async def extract_profile(llm: LLM, path: Path, text: str) -> ResumeExtraction:
    if path.suffix.lower() == ".pdf" and path.stat().st_size <= MAX_RESUME_BYTES:
        data = base64.standard_b64encode(path.read_bytes()).decode()
        content: list[dict] = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
            {"type": "text", "text": "Extract the profile from this resume."},
        ]
    else:
        content = [
            {"type": "text", "text": f"<resume>\n{text}\n</resume>\n\nExtract the profile from this resume."}
        ]
    response = await llm.create(
        system=RESUME_SYSTEM,
        messages=[{"role": "user", "content": content}],
        max_tokens=8000,
        effort="low",
        output_format=ResumeExtraction,
    )
    extraction = getattr(response, "parsed_output", None)
    if extraction is None:
        raise ValueError(f"no profile returned (stop reason: {response.stop_reason})")
    return extraction


def merge_extraction(profile: Profile, extraction: ResumeExtraction) -> list[str]:
    """Fill empty profile fields from the extraction. Never overwrites what you typed. Returns
    the names of the fields that changed."""
    changed: list[str] = []
    simple = [
        "first_name", "last_name", "email", "phone", "city", "state", "country", "linkedin_url",
        "github_url", "portfolio_url", "current_title", "current_company", "summary",
    ]  # fmt: skip
    for name in simple:
        value = (getattr(extraction, name) or "").strip()
        if value and not getattr(profile, name):
            setattr(profile, name, value)
            changed.append(name)
    if profile.years_experience is None and extraction.years_experience is not None:
        profile.years_experience = round(float(extraction.years_experience), 1)
        changed.append("years_experience")

    def merge_list(name: str, values: list[str]) -> None:
        current = list(getattr(profile, name))
        seen = {v.lower() for v in current}
        added = [v.strip() for v in values if v.strip() and v.strip().lower() not in seen]
        if added:
            setattr(profile, name, current + added)
            changed.append(name)

    merge_list("skills", extraction.skills)
    merge_list("certifications", extraction.certifications)
    merge_list("languages", extraction.languages)
    if not profile.target_titles and extraction.suggested_target_titles:
        profile.target_titles = [t.strip() for t in extraction.suggested_target_titles if t.strip()]
        changed.append("target_titles")
    if not profile.education and extraction.education:
        profile.education = [Education.model_validate(e.model_dump()) for e in extraction.education]
        changed.append("education")
    return changed

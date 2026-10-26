"""Profile: your details, resume and saved application answers."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from jobbot.ai import LLM, describe_error
from jobbot.ai.resume import (
    ALLOWED_EXTENSIONS,
    MAX_RESUME_BYTES,
    extract_profile,
    extract_text,
    merge_extraction,
)
from jobbot.config import anthropic_credentials_present
from jobbot.db import get_profile, get_resume, get_settings, save_profile, save_resume
from jobbot.models import utcnow
from jobbot.pipeline import rescore_all
from jobbot.schemas import WORKPLACES, Education, Profile, QAPair
from jobbot.web.app import get_db, render

router = APIRouter()

EEO_OPTIONS = {
    "gender": ["", "Female", "Male", "Non-binary"],
    "race_ethnicity": [
        "",
        "American Indian or Alaska Native",
        "Asian",
        "Black or African American",
        "Hispanic or Latino",
        "Native Hawaiian or Other Pacific Islander",
        "White",
        "Two or More Races",
    ],
    "veteran_status": [
        "",
        "I am not a protected veteran",
        "I identify as one or more of the classifications of protected veteran",
    ],
    "disability_status": [
        "",
        "No, I don't have a disability",
        "Yes, I have a disability (or previously had one)",
    ],
}
MESSAGES = {
    "saved": "Profile saved. Filters and scores were updated.",
    "uploaded": "Resume uploaded.",
    "uploaded_thin": "Resume uploaded, but very little text could be read from it (is it a scanned image?). "
    "Matching and the AI work best with a text-based PDF or DOCX.",
}


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _items(value: str) -> list[str]:
    seen, out = set(), []
    for item in re.split(r"[\n,;]", value):
        item = item.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def _rows(form: FormData, prefix: str, fields: list[str]) -> list[dict[str, str]]:
    """Collect repeatable rows named like `qa-<id>-question`."""
    ids: list[str] = []
    for key in form:
        m = re.fullmatch(rf"{prefix}-([\w]+)-({'|'.join(fields)})", key)
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return [{f: str(form.get(f"{prefix}-{i}-{f}", "")).strip() for f in fields} for i in ids]


def profile_from_form(form: FormData, current: Profile) -> Profile:
    text = lambda name: str(form.get(name, "")).strip()  # noqa: E731
    years = text("years_experience")
    salary = re.sub(r"[^\d]", "", text("desired_salary"))
    data = current.model_dump()
    data.update(
        {
            name: text(name)
            for name in (
                "first_name",
                "last_name",
                "preferred_name",
                "email",
                "phone",
                "address",
                "city",
                "state",
                "postal_code",
                "country",
                "linkedin_url",
                "github_url",
                "portfolio_url",
                "other_url",
                "current_title",
                "current_company",
                "summary",
                "work_authorization",
                "security_clearance",
                "salary_currency",
                "salary_expectation_text",
                "notice_period",
                "earliest_start_date",
                "gender",
                "race_ethnicity",
                "veteran_status",
                "disability_status",
                "pronouns",
                "how_did_you_hear",
                "agent_instructions",
            )
        }  # fmt: skip
    )
    data["salary_currency"] = data["salary_currency"].upper() or "USD"
    data["years_experience"] = float(years) if re.fullmatch(r"\d+(\.\d+)?", years) else None
    data["desired_salary"] = int(salary) if salary else None
    data["target_titles"] = _lines(text("target_titles"))
    data["skills"] = _items(text("skills"))
    data["certifications"] = _items(text("certifications"))
    data["languages"] = _items(text("languages"))
    data["requires_sponsorship"] = form.get("requires_sponsorship") == "yes"
    data["willing_to_relocate"] = form.get("willing_to_relocate") == "yes"
    data["workplace_preferences"] = [w for w in WORKPLACES if form.get(f"workplace_{w}")] or list(WORKPLACES)
    data["education"] = [
        Education(**row).model_dump()
        for row in _rows(form, "edu", ["school", "degree", "field_of_study", "graduation_year"])
        if row["school"] or row["degree"]
    ]
    data["qa"] = [
        QAPair(**row).model_dump()
        for row in _rows(form, "qa", ["question", "answer"])
        if row["question"] and row["answer"]
    ]
    return Profile.model_validate(data)


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, msg: str = "", filled: str = "", error: str = "") -> Response:
    with get_db(request).session() as s:
        profile = get_profile(s)
        resume = get_resume(s)
        settings = get_settings(s)
    message = MESSAGES.get(msg, "")
    if msg == "autofill":
        fields = [f.replace("_", " ") for f in filled.split(",") if f]
        message = (
            f"Filled in from your resume: {', '.join(fields)}. Review everything below and save."
            if fields
            else "Your profile already had everything the resume contained."
        )
    return render(
        request,
        "profile.html",
        active="profile",
        title="Profile",
        profile=profile,
        resume=resume,
        message=message,
        error=error,
        eeo=EEO_OPTIONS,
        workplaces=WORKPLACES,
        ai_available=settings.ai.enabled and anthropic_credentials_present(),
    )


@router.post("/profile")
async def save_profile_form(request: Request) -> Response:
    form = await request.form()
    db = get_db(request)
    with db.session() as s:
        profile = profile_from_form(form, get_profile(s))
        save_profile(s, profile)
    rescore_all(db)
    return RedirectResponse("/profile?msg=saved", status_code=303)


@router.post("/profile/resume")
async def upload_resume(request: Request, file: UploadFile = File(...)) -> Response:
    env = request.app.state.env
    name = Path(file.filename or "resume").name
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return RedirectResponse("/profile?error=Upload+a+PDF,+DOCX+or+TXT+file", status_code=303)
    data = await file.read(MAX_RESUME_BYTES + 1)
    if len(data) > MAX_RESUME_BYTES:
        return RedirectResponse("/profile?error=That+file+is+larger+than+10+MB", status_code=303)
    safe = re.sub(r"[^\w .()-]+", "_", Path(name).stem).strip() or "resume"
    target = env.resume_dir / f"{safe}{suffix}"
    db = get_db(request)
    with db.session() as s:
        previous = get_resume(s)
    if previous.get("path") and Path(previous["path"]) != target:
        Path(previous["path"]).unlink(missing_ok=True)
    target.write_bytes(data)
    try:
        text = extract_text(target)
    except Exception:
        text = ""
    with db.session() as s:
        save_resume(
            s,
            {"filename": target.name, "path": str(target), "text": text, "uploaded_at": utcnow().isoformat()},
        )
    rescore_all(db)
    return RedirectResponse(
        f"/profile?msg={'uploaded' if len(text) >= 200 else 'uploaded_thin'}", status_code=303
    )


@router.get("/profile/resume/file")
def download_resume(request: Request) -> Response:
    with get_db(request).session() as s:
        resume = get_resume(s)
    path = Path(resume.get("path", ""))
    if not resume or not path.is_file():
        raise HTTPException(404, "No resume uploaded")
    return FileResponse(path, filename=resume.get("filename") or path.name)


@router.post("/profile/resume/autofill")
async def autofill(request: Request) -> Response:
    db = get_db(request)
    with db.session() as s:
        resume = get_resume(s)
        settings = get_settings(s)
    path = Path(resume.get("path", ""))
    if not resume or not path.is_file():
        return RedirectResponse("/profile?error=Upload+your+resume+first", status_code=303)
    try:
        async with LLM.open(settings.ai) as llm:
            extraction = await extract_profile(llm, path, resume.get("text", ""))
    except Exception as exc:
        from urllib.parse import quote_plus

        return RedirectResponse(f"/profile?error={quote_plus(describe_error(exc))}", status_code=303)
    with db.session() as s:
        profile = get_profile(s)
        changed = merge_extraction(profile, extraction)
        save_profile(s, profile)
    rescore_all(db)
    return RedirectResponse(f"/profile?msg=autofill&filled={','.join(changed)}", status_code=303)

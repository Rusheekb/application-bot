"""Quick fill: fills in an application from your profile with plain matching rules. No AI, so
no API key and no cost.

It answers the questions it recognizes (name, contact details, links, location, education,
current job, resume and cover letter uploads, work authorization and sponsorship, voluntary
self-identification, "how did you hear about us", salary, notice period and start date) and
anything saved under Profile → Saved answers. Required questions it can't answer become
questions for you, and your answers can be saved for next time. Like the agent, it stops for
your review and only clicks submit after you approve.

What it can't do without a model: write answers to open-ended questions, judge skill-specific
questions ("years of experience with Spark"), or find its way through unusual forms.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from jobbot.apply.agent import AgentOutcome
from jobbot.apply.browser import BrowserSession, ToolError, _is_empty, best_option
from jobbot.models import utcnow
from jobbot.schemas import Profile
from jobbot.text import truncate

log = logging.getLogger(__name__)

DECLINE = "Decline to self-identify"
DECLINE_RE = re.compile(
    r"decline|prefer not|don'?t wish|do not wish|not (?:to )?(?:say|answer|disclose)|choose not|rather not|"
    r"wish not to",
    re.I,
)
OPEN_FORM_RE = re.compile(r"\bapply\b|start (?:your |my |the )?application|i'?m interested", re.I)
CONSENT_RE = re.compile(
    r"privacy|consent|acknowledg|i (?:have read|agree|certify|understand|confirm|accept|attest)|terms|"
    r"data (?:processing|protection)|gdpr",
    re.I,
)
# Work authorization text that says the candidate isn't authorized (sponsorship has its own flag).
NOT_AUTHORIZED_RE = re.compile(
    r"\bnot (?:yet |currently )?(?:authori[sz]ed|eligible|permitted)\b|\bunauthori[sz]ed\b|"
    r"\bno (?:work )?(?:permit|authori[sz]ation|visa)\b|\bneeds? (?:a )?(?:visa|work permit|work authori[sz]ation)\b",
    re.I,
)
_COUNTRIES = {
    "us": r"united states(?: of america)?|u\.s\.a?\.?|usa|\bus\b|america",
    "ca": r"canada",
    "uk": r"united kingdom|\bu\.?k\.?\b|great britain|britain|england",
    "eu": r"european union|\beu\b|europe",
    "de": r"germany",
    "fr": r"france",
    "nl": r"netherlands",
    "ie": r"ireland",
    "es": r"spain",
    "in": r"india",
    "au": r"australia",
    "nz": r"new zealand",
    "sg": r"singapore",
    "il": r"israel",
    "mx": r"mexico",
}
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "could", "do", "does", "for", "from", "have", "how",
    "i", "if", "in", "is", "it", "of", "on", "or", "our", "please", "the", "this", "to", "us", "we", "what",
    "when", "where", "which", "who", "will", "with", "would", "you", "your",
}  # fmt: skip


def clean_label(label: str) -> str:
    """Lower-case question text without required markers, option hints or punctuation."""
    text = re.sub(r"\s*\(options?:.*\)\s*$", "", label or "", flags=re.I)
    text = re.sub(r"[*✱]|\((?:required|optional)\)", " ", text.lower())
    text = re.sub(r"[^\w\s/+#&'.-]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" .-")


def _tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9+#]+", text.lower())
        if t.isdigit() or (len(t) > 2 and t not in _STOPWORDS)
    }


def _countries(text: str) -> set[str]:
    return {code for code, pattern in _COUNTRIES.items() if re.search(pattern, text, re.I)}


def _yes(flag: bool) -> str:
    return "Yes" if flag else "No"


class Answers:
    """Decides what goes in a form field. `None` means it doesn't know, and should ask."""

    def __init__(
        self,
        profile: Profile,
        *,
        files: set[str],
        allow_consent: bool,
        saved: dict[str, str] | None = None,
    ):
        self.profile = profile
        self.files = files
        self.allow_consent = allow_consent
        self.saved: dict[str, str] = {qa.question: qa.answer for qa in profile.qa if qa.answer.strip()}
        self.saved.update(saved or {})

    # -- saved answers -----------------------------------------------------------------------

    def saved_answer(self, label: str) -> str | None:
        """An answer saved for this question, or for one worded almost the same way."""
        target = clean_label(label)
        target_tokens = _tokens(target)
        for question, answer in self.saved.items():
            if clean_label(question) == target:
                return answer
        best, best_score = None, 0.0
        for question, answer in self.saved.items():
            tokens = _tokens(clean_label(question))
            if not tokens or not target_tokens:
                continue
            score = len(tokens & target_tokens) / len(tokens | target_tokens)
            if score > best_score:
                best, best_score = answer, score
        return best if best_score >= 0.8 else None

    # -- rules -----------------------------------------------------------------------------------

    def _same_country(self, label: str) -> bool:
        """The question is about the country the profile's work authorization covers (or none)."""
        asked = _countries(label)
        mine = _countries(f"{self.profile.work_authorization} {self.profile.country}")
        return not asked or bool(asked & mine)

    def _authorized(self, label: str) -> bool:
        """The profile says the candidate can work where the question asks about."""
        status = self.profile.work_authorization.strip()
        return bool(status) and self._same_country(label) and not NOT_AUTHORIZED_RE.search(status)

    def _rules(self) -> list[tuple[str, Callable[[str], str | None]]]:
        p = self.profile
        edu = p.education[0] if p.education else None
        salary = p.salary_expectation_text or (
            f"{p.desired_salary:,} {p.salary_currency}".strip() if p.desired_salary else ""
        )

        def sponsorship(label: str) -> str | None:
            # Only once the profile covers eligibility: an unticked "needs sponsorship" box alone
            # doesn't say much.
            if not p.work_authorization.strip() or not self._same_country(label):
                return None
            if re.search(r"without (?:\w+ ){0,4}sponsor", label):
                # "Are you authorized to work in the US without sponsorship?"
                if p.requires_sponsorship:
                    return "No"
                return "Yes" if self._authorized(label) else None
            return _yes(p.requires_sponsorship)

        def authorized(label: str) -> str | None:
            return "Yes" if self._authorized(label) else None

        def clearance(label: str) -> str | None:
            return _yes(bool(p.security_clearance.strip()))

        def years(label: str) -> str | None:
            return f"{p.years_experience:g}" if p.years_experience is not None else None

        return [
            # First, since its options often name sites ("LinkedIn", "Company website").
            (
                r"\bhow did you (?:hear|find|learn)(?: out)? about\b|\bwhere did you (?:hear|find|see|learn)\b"
                r"|\breferral source\b",
                lambda _: p.how_did_you_hear,
            ),
            # Identity and contact details.
            (r"\bpreferred (?:first )?name\b|\bnick ?name\b", lambda _: p.preferred_name or p.first_name),
            (r"\b(?:first|given) name\b|\bforename\b", lambda _: p.first_name),
            (r"\b(?:last|family) name\b|\bsurname\b", lambda _: p.last_name),
            (
                r"^(?:your |full |legal |candidate )?(?:full )?name$|\b(?:full|legal) name\b",
                lambda _: p.full_name,
            ),
            (r"\be-?mail\b", lambda _: p.email),
            (
                r"\b(?:phone|telephone)(?: number)?\b|^(?:mobile|cell)(?: phone| number)?$"
                r"|\b(?:mobile|cell) (?:phone|number)\b",
                lambda _: p.phone,
            ),
            (
                r"^linked ?in(?: profile)?(?: url| link)?$|\blinked ?in (?:profile|url|link)\b",
                lambda _: p.linkedin_url,
            ),
            (
                r"^github(?: profile| username)?(?: url| link)?$|\bgithub (?:profile|url|link)\b",
                lambda _: p.github_url,
            ),
            (
                r"^(?:personal |portfolio )?(?:website|portfolio|blog)(?: url| link)?$|\bpersonal (?:web)?site\b"
                r"|\bportfolio (?:url|link|website)\b",
                lambda _: p.portfolio_url or p.other_url,
            ),
            # Location.
            (r"^(?:street |home |mailing )?address(?: line 1)?$", lambda _: p.address),
            (r"^(?:current )?city$", lambda _: p.city),
            (r"^(?:state|province|state ?/ ?province|state or province)$", lambda _: p.state),
            (r"\b(?:zip|postal)(?: code)?\b", lambda _: p.postal_code),
            (r"^(?:country|country of residence|country ?/ ?region)$", lambda _: p.country),
            (
                r"^(?:current |your )?location(?: city)?(?: state)?(?: country)?$"
                r"|\bwhere are you (?:currently )?(?:located|based)\b|\bcity,? (?:and )?(?:state|country)\b",
                lambda _: p.location_text,
            ),
            # Work and education.
            (
                r"\b(?:current|most recent|present) (?:company|employer)\b|^(?:company|employer)$",
                lambda _: p.current_company,
            ),
            (
                r"\b(?:current|most recent|present) (?:job )?title\b|^(?:job )?title$",
                lambda _: p.current_title,
            ),
            (
                r"^(?:total |how many )?years of (?:professional |relevant |total |work |industry )?experience"
                r"(?: do you have)?$",
                years,
            ),
            (
                r"^(?:school|university|college|institution)(?: name)?$|\bname of (?:your )?(?:school|university|college)\b",
                lambda _: edu.school if edu else "",
            ),
            (
                r"^(?:degree|degree type|degree level)$|\bhighest (?:level of )?(?:degree|education)\b",
                lambda _: edu.degree if edu else "",
            ),
            (
                r"\b(?:discipline|major|field of study|area of study|concentration)\b",
                lambda _: edu.field_of_study if edu else "",
            ),
            (
                r"\bgraduation (?:year|date)\b|\byear of graduation\b",
                lambda _: edu.graduation_year if edu else "",
            ),
            # Logistics.
            (
                r"\bsalary expectations?\b|\b(?:expected|desired|target) (?:salary|pay|compensation)\b"
                r"|\b(?:salary|compensation|pay) (?:expectations?|requirements?|range)\b",
                lambda _: salary,
            ),
            (r"\bnotice period\b", lambda _: p.notice_period),
            (
                r"\bstart date\b|\bwhen (?:can|could|would) you start\b|\bavailab(?:le|ility) to start\b"
                r"|\bearliest (?:start|availability)\b",
                lambda _: p.earliest_start_date,
            ),
            # Eligibility.
            (r"\bsponsor", sponsorship),
            (r"\b(?:authori[sz]ed|eligible|legally (?:able|permitted|entitled)|right) to work\b", authorized),
            (r"\b(?:have|hold|possess)\b.*\bclearance\b|\bactive (?:security )?clearance\b", clearance),
            (r"\brelocat", lambda _: "Yes" if p.willing_to_relocate else None),
            # Voluntary self-identification: blank in the profile means "decline".
            (r"\bpronouns?\b", lambda _: p.pronouns),
            (r"\bgender\b|^sex$", lambda _: p.gender or DECLINE),
            (r"\b(?:race|ethnicity|hispanic|latino|latina|latinx)\b", lambda _: p.race_ethnicity or DECLINE),
            (r"\bveteran\b", lambda _: p.veteran_status or DECLINE),
            (r"\bdisabilit", lambda _: p.disability_status or DECLINE),
            (r"\b(?:sexual orientation|transgender|lgbtq?)\b", lambda _: DECLINE),
        ]

    def value_for(self, field: dict[str, Any]) -> str | None:
        """The value to fill in, already matched to one of the field's options when it has them."""
        kind, label = field["kind"], field.get("label") or ""
        text = clean_label(label)
        if not text:
            return None
        if kind == "file":
            if re.search(r"\b(?:resume|cv|curriculum)\b", text) and "resume" in self.files:
                return "resume"
            if re.search(r"\bcover letter\b", text) and "cover_letter" in self.files:
                return "cover_letter"
            return None
        if (saved := self.saved_answer(label)) is not None:
            return self._fit(field, saved)
        if kind == "checkbox":
            return "true" if self.allow_consent and CONSENT_RE.search(text) else None
        if kind == "checkboxes":
            return None  # pick-several questions need the candidate
        for pattern, rule in self._rules():
            if re.search(pattern, text):
                value = rule(text)
                return self._fit(field, value) if value else None
        return None

    @staticmethod
    def _fit(field: dict[str, Any], value: str) -> str | None:
        """Translate a value into one of the field's options (or a fill instruction)."""
        options = [o["label"] for o in field.get("options") or [] if o.get("label")]
        if field["kind"] == "checkbox":
            return "true" if value.strip().lower() in ("true", "yes", "checked", "y", "1") else "false"
        if value == DECLINE:
            if options:
                return next((o for o in options if DECLINE_RE.search(o)), None)
            return DECLINE if field["kind"] == "combobox" else None
        if options:
            return best_option(value, options)
        return value


class QuickFiller:
    """Fills an application without a model. Same interface as ApplyAgent, for ApplyManager."""

    llm = None

    def __init__(
        self,
        session: BrowserSession,
        *,
        profile: Profile,
        files: set[str],
        allow_consent: bool,
        mode: str,
        on_step: Callable[[dict[str, Any]], None],
        previous_answers: list[dict] | None = None,
    ):
        self.session = session
        self.mode = mode
        self.on_step = on_step
        saved = {}
        for answer in previous_answers or []:  # the candidate approved these in an earlier run
            value = str(answer.get("value") or "")
            if answer.get("kind") == "file" or not value:
                continue
            if answer.get("kind") == "checkbox":
                value = "true" if value == "Checked" else "false"
            saved[str(answer.get("label") or "")] = value
        self.answers = Answers(profile, files=files, allow_consent=allow_consent, saved=saved)
        self.unanswered_buttons: list[str] = []

    # -- the ApplyAgent interface --------------------------------------------------------------

    async def set_mode(self, mode: str) -> None:
        self.mode = mode
        await self.session.set_mode(mode)

    def add_answers(self, answers: dict[str, str]) -> None:
        self.answers.saved.update(answers)

    def stopped(self, reason: str) -> AgentOutcome:
        if self.session.submitted:
            return AgentOutcome(
                "submitted", f"The page confirmed the application was received, but then: {reason}"
            )
        if self.session.submit_attempts:
            return AgentOutcome(
                "failed",
                f"{reason} Submit was already clicked, so check whether the application went through "
                "(look for a confirmation email) before trying again.",
            )
        return AgentOutcome("failed", reason)

    async def start(self) -> AgentOutcome:
        state = await self._find_form()
        if isinstance(state, AgentOutcome):
            return state
        outcome = await self._fill(state)
        if outcome.status == "ready_for_review" and self.mode == "submit":
            return await self._submit()  # re-running an application the candidate approved
        return outcome

    async def resume(self, message: str) -> AgentOutcome:
        """After an approval: submit. After answers: fill them in and check again."""
        if self.mode == "submit":
            self._log("user", "Approved: submit the application.")
            return await self._submit()
        self._log("user", "Answered the questions.")
        return await self._fill(await self.session.snapshot())

    # -- steps -----------------------------------------------------------------------------------

    def _log(self, kind: str, text: str, **extra: Any) -> None:
        entry = {
            "t": utcnow().isoformat(timespec="seconds"),
            "kind": kind,
            "text": truncate(text, 2000),
            **extra,
        }
        try:
            self.on_step(entry)
        except Exception:
            log.exception("Step logging failed")

    async def _find_form(self) -> dict | AgentOutcome:
        """Read the page; on a job description, follow its Apply button to the form."""
        for _ in range(3):
            self._log("tool", "get_page_state")
            state = await self.session.snapshot()
            if any("visible challenge" in c for c in state.get("captcha") or []):
                return AgentOutcome("blocked", "The page shows a CAPTCHA. Open it and apply yourself.")
            if state.get("login"):
                return AgentOutcome("blocked", "The site asks you to sign in. Open it and apply yourself.")
            fields = state["fields"]
            if any(f["kind"] == "file" for f in fields) or sum(f["kind"] != "checkbox" for f in fields) >= 3:
                return state
            opener = next(
                (
                    b
                    for b in state["buttons"]
                    if OPEN_FORM_RE.search(b["label"]) and not b.get("final_submit")
                ),
                None,
            )
            if opener is None:
                break
            self._log("tool", "click", detail=f"[{opener['ref']}] {opener['label']}")
            try:
                self._log("result", await self.session.click(opener["ref"]))
            except Exception as exc:
                self._log("result", str(exc).splitlines()[0][:300], error=True)
                break
        return AgentOutcome(
            "failed", "Couldn't find an application form on this page. Open the posting and apply yourself."
        )

    async def _fill(self, state: dict) -> AgentOutcome:
        tried: set[str] = set()
        self.unanswered_buttons = []
        for _ in range(3):  # answers can reveal follow-up questions
            actions = []
            for field in state["fields"]:
                if field["ref"] in tried or not _is_empty(field):
                    continue
                tried.add(field["ref"])
                value = self.answers.value_for(field)
                if value == DECLINE and field["kind"] == "combobox":
                    value = await self._decline_option(field)  # its wording varies from form to form
                if value is not None:
                    actions.append({"ref": field["ref"], "value": value})
            clicks = self._answer_buttons(state, tried)
            if not actions and not clicks:
                break
            if actions:
                self._log(
                    "tool",
                    "fill_fields",
                    detail=", ".join(f"[{a['ref']}]={a['value'][:40]!r}" for a in actions),
                )
                self._log("result", await self.session.fill(actions))
            for ref, label in clicks:
                self._log("tool", "click", detail=f"[{ref}] {label}")
                try:
                    self._log("result", await self.session.click(ref))
                except Exception as exc:  # an answer that didn't take is left for the candidate
                    self._log("result", str(exc).splitlines()[0][:300], error=True)
            self._log("tool", "get_page_state")
            state = await self.session.snapshot()

        missing = [f for f in state["fields"] if f.get("required") and _is_empty(f)]
        filled = sum(1 for f in state["fields"] if not _is_empty(f))
        if missing:
            questions = [self._question(f) for f in missing]
            summary = (
                f"Filled in {filled} field{'s' if filled != 1 else ''} from your profile. Answer the questions "
                "below to finish (or open the form and do it yourself)."
            )
            self._log("finish", f"needs_input: {summary}")
            return AgentOutcome("needs_input", summary, missing_info=questions)
        summary = f"Filled in {filled} field{'s' if filled != 1 else ''} from your profile and saved answers."
        if self.unanswered_buttons:
            summary += " Not answered: " + "; ".join(self.unanswered_buttons[:5]) + "."
        summary += " Check everything, especially optional questions it left empty, before submitting."
        self._log("finish", f"ready_for_review: {summary}")
        return AgentOutcome("ready_for_review", summary)

    async def _decline_option(self, field: dict) -> str | None:
        try:
            listed = await self.session.list_options(field["ref"])
        except Exception as exc:  # a dropdown that won't open: leave the question to the candidate
            log.debug("Couldn't list options for %s: %s", field.get("label"), exc)
            return None
        return next((o.strip() for o in listed.split(" | ") if DECLINE_RE.search(o)), None)

    def _answer_buttons(self, state: dict, tried: set[str]) -> list[tuple[str, str]]:
        """Questions shown as a row of buttons (Yes / No) that the profile answers."""
        groups: dict[str, list[dict]] = {}
        for button in state["buttons"]:
            if button.get("context") and not button.get("final_submit") and not button.get("may_submit"):
                groups.setdefault(button["context"], []).append(button)
        clicks = []
        for question, buttons in groups.items():
            key = f"buttons:{question}"
            if key in tried or len(buttons) < 2 or any(b.get("pressed") for b in buttons):
                continue
            tried.add(key)
            field = {"kind": "radio", "label": question, "options": [{"label": b["label"]} for b in buttons]}
            choice = self.answers.value_for(field)
            button = next((b for b in buttons if b["label"] == choice), None)
            if button is None:
                self.unanswered_buttons.append(truncate(question, 100))
                continue
            clicks.append((button["ref"], button["label"]))
        return clicks

    @staticmethod
    def _question(field: dict) -> str:
        label = re.sub(r"[\s*✱]+$", "", field.get("label") or "(unlabeled field)").strip()
        options = [o["label"] for o in field.get("options") or [] if o.get("label")]
        if options:
            return f"{label} (options: {' | '.join(options[:12])})"
        return label

    async def _submit(self) -> AgentOutcome:
        self._log("tool", "get_page_state")
        state = await self.session.snapshot()
        missing = [f for f in state["fields"] if f.get("required") and _is_empty(f)]
        if missing:
            return AgentOutcome(
                "needs_input",
                "Some required questions are still empty.",
                missing_info=[self._question(f) for f in missing],
            )
        button = next((b for b in state["buttons"] if b.get("final_submit") and not b.get("disabled")), None)
        if button is None:
            return AgentOutcome(
                "blocked", "Couldn't find the form's submit button. Open the form and submit it yourself."
            )
        self.session.submit_attempts += 1
        self._log("tool", "submit_application", detail=f"[{button['ref']}] {button['label']}")
        try:
            result = await self.session.submit(button["ref"])
        except ToolError as exc:
            self._log("result", str(exc), error=True)
            return self.stopped(f"Submitting failed: {exc}.")
        self._log("result", result)
        if self.session.submitted:
            self._log("finish", "submitted")
            return AgentOutcome("submitted", "Submitted. The page confirmed the application was received.")
        state = await self.session.snapshot()
        missing = [f for f in state["fields"] if f.get("required") and _is_empty(f)]
        if missing:
            return AgentOutcome(
                "needs_input",
                "The form wasn't accepted: some required questions are still empty.",
                missing_info=[self._question(f) for f in missing],
            )
        alerts = "; ".join(state.get("alerts") or [])
        return self.stopped(
            f"Clicked submit, but the page didn't confirm it{f' ({alerts})' if alerts else ''}."
        )

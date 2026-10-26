"""Browser actions for the apply agent, implemented with Playwright.

The agent never sees raw HTML: it gets a compact list of form fields, buttons and alerts (from
snapshot.js), and acts on elements by their `ref` number.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SNAPSHOT_JS = Path(__file__).with_name("snapshot.js").read_text()

# Injected into every page in review mode: blocks real form submission as a second line of
# defence behind the click guard (catches Enter-key submits and scripted submits).
REVIEW_GUARD_JS = """
(() => {
  window.__jobbotReview = true;
  const looksLikeApplication = (form) => form && (
    form.querySelector('input[type=file]') ||
    form.querySelectorAll('input:not([type=hidden]), textarea, select').length >= 3);
  document.addEventListener('submit', (event) => {
    if (window.__jobbotReview && looksLikeApplication(event.target)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      window.__jobbotBlockedSubmits = (window.__jobbotBlockedSubmits || 0) + 1;
    }
  }, true);
})();
"""

CAPTCHA_FRAME_RE = re.compile(
    r"recaptcha|hcaptcha\.com|challenges\.cloudflare\.com|arkoselabs|funcaptcha|captcha-delivery", re.I
)
CONFIRMATION_RE = re.compile(
    r"thank(?:s| you)[^.\n]{0,60}(?:appl|submi|interest)"
    r"|application (?:has been |was |is )?(?:successfully )?(?:submitted|received|sent|complete)"
    r"|we(?:'ve| have) (?:successfully )?received your application"
    r"|successfully (?:submitted|applied)"
    r"|you(?:'ve| have) (?:successfully )?applied",
    re.I,
)
TEXT_KINDS = {"text", "email", "tel", "url", "textarea", "date", "month", "number"}
MAX_STATE_CHARS = 16000


class ToolError(Exception):
    """An action failed in a way the agent should be told about."""


def _norm(s: str) -> str:
    s = (s or "").lower().replace("✱", "").replace("*", "")
    s = re.sub(r"[^\w\s/+#&'-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def best_option(value: str, labels: list[str]) -> str | None:
    """Pick the option label that best matches the requested value."""
    if not labels:
        return None
    v = _norm(value)
    normed = [(_norm(label), label) for label in labels]
    for n, label in normed:
        if n == v:
            return label
    if v in ("yes", "no", "true", "false"):
        want = "yes" if v in ("yes", "true") else "no"
        starts = [label for n, label in normed if n.split(" ")[:1] == [want] or n.startswith(want + ",")]
        if starts:
            return min(starts, key=len)
    prefix = [label for n, label in normed if v and n.startswith(v)]
    if prefix:
        return min(prefix, key=len)
    contains = [label for n, label in normed if v and v in n]
    if contains:
        return min(contains, key=len)
    contained = [label for n, label in normed if n and len(n) > 2 and n in v]
    if contained:
        return max(contained, key=len)
    scored = [(difflib.SequenceMatcher(None, v, n).ratio(), label) for n, label in normed]
    ratio, label = max(scored)
    return label if ratio >= 0.72 else None


def _short_error(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:240]


class BrowserSession:
    """One application attempt in one browser context."""

    def __init__(self, context: Any, page: Any, *, files: dict[str, Path], mode: str):
        self.context = context
        self.page = page
        self.files = files
        self.mode = mode
        self.next_ref = 1
        self.fields: dict[str, dict] = {}
        self.options: dict[str, dict] = {}  # option ref -> its group field
        self.buttons: dict[str, dict] = {}
        self.ref_frame: dict[str, Any] = {}
        self.submitted = False
        self.submit_attempts = 0
        self.last_snapshot: dict | None = None
        context.on("page", self._on_new_page)

    def _on_new_page(self, page: Any) -> None:
        # "Apply" buttons sometimes open the application form in a new tab.
        self.page = page

    async def settle(self, timeout_ms: int = 4000) -> None:
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        for state, limit in (("domcontentloaded", timeout_ms), ("networkidle", min(2500, timeout_ms))):
            with contextlib.suppress(PlaywrightTimeout):
                await self.page.wait_for_load_state(state, timeout=limit)
        await asyncio.sleep(0.3)

    # -- reading the page --------------------------------------------------------------------

    async def snapshot(self) -> dict:
        await self.settle()
        combined: dict[str, Any] = {
            "url": self.page.url,
            "title": "",
            "headings": [],
            "fields": [],
            "buttons": [],
            "alerts": [],
            "captcha": [],
            "login": False,
            "text": "",
        }
        self.fields, self.options, self.buttons = {}, {}, {}
        for frame in self.page.frames:
            if frame.is_detached() or (
                frame != self.page.main_frame and CAPTCHA_FRAME_RE.search(frame.url or "")
            ):
                continue
            is_main = frame == self.page.main_frame
            try:
                data = await frame.evaluate(
                    SNAPSHOT_JS,
                    {"startRef": self.next_ref, "maxOptions": 60, "maxText": 1500 if is_main else 300},
                )
            except Exception as exc:  # frames can navigate away mid-read
                log.debug("Snapshot failed in frame %s: %s", frame.url, exc)
                continue
            self.next_ref = max(self.next_ref, int(data.get("nextRef") or self.next_ref))
            if is_main:
                combined["title"] = data.get("title", "")
                combined["text"] = data.get("text", "")
                combined["headings"] = data.get("headings", [])
            elif data.get("fields"):
                combined["text"] += f"\n[embedded form from {frame.url[:80]}]"
            combined["login"] = combined["login"] or bool(data.get("login"))
            combined["captcha"] += data.get("captcha", [])
            combined["alerts"] += data.get("alerts", [])
            for field in data.get("fields", []):
                combined["fields"].append(field)
                self.fields[field["ref"]] = field
                self.ref_frame[field["ref"]] = frame
                for option in field.get("options") or []:
                    if "ref" in option:
                        self.options[option["ref"]] = field
                        self.ref_frame[option["ref"]] = frame
            for button in data.get("buttons", []):
                combined["buttons"].append(button)
                self.buttons[button["ref"]] = button
                self.ref_frame[button["ref"]] = frame
        self.last_snapshot = combined
        return combined

    async def page_state_text(self) -> str:
        return format_state(await self.snapshot())

    def _locator(self, ref: str) -> Any:
        frame = self.ref_frame.get(ref)
        if frame is None or frame.is_detached():
            raise ToolError(f"unknown or stale ref {ref}; call get_page_state to refresh")
        return frame.locator(f'[data-jobbot-ref="{ref}"]').first

    # -- acting on the page --------------------------------------------------------------------

    async def fill(self, actions: list[dict]) -> str:
        from playwright.async_api import Error as PlaywrightError

        if not actions:
            return "No actions given."
        lines = []
        for action in actions:
            ref = str(action.get("ref", "")).strip()
            value = str(action.get("value", ""))
            field = self.fields.get(ref) or self.options.get(ref)
            if field is None:
                lines.append(f"[{ref}] error: unknown ref; call get_page_state to refresh")
                continue
            try:
                result = await self._fill_one(field, ref, value)
            except ToolError as exc:
                result = f"error: {exc}"
            except PlaywrightError as exc:
                result = f"error: {_short_error(exc)}"
            lines.append(f"[{ref}] {field.get('label', '')[:70]!r}: {result}")
        return "\n".join(lines)

    async def _fill_one(self, field: dict, ref: str, value: str) -> str:
        kind = field["kind"]
        if kind in TEXT_KINDS:
            loc = self._locator(field["ref"])
            if kind == "number":
                value = re.sub(r"[^0-9.\-]", "", value)
            await loc.fill(value, timeout=6000)
            actual = await loc.input_value(timeout=3000)
            if actual.strip() != value.strip():
                return f"filled, but the field now shows {actual[:80]!r}"
            return "filled"
        if kind == "richtext":
            loc = self._locator(field["ref"])
            await loc.fill(value, timeout=6000)
            return "filled"
        if kind in ("select", "multiselect"):
            return await self._select(field, value)
        if kind == "radio":
            option = self._match_option(field, value, ref)
            await self._set_checked(option["ref"], True)
            return f"selected {option['label']!r}"
        if kind == "checkboxes":
            option = self._match_option(field, value, ref)
            want = not value.strip().startswith("!")
            await self._set_checked(option["ref"], want)
            return f"{'checked' if want else 'unchecked'} {option['label']!r}"
        if kind == "checkbox":
            want = value.strip().lower() in ("true", "yes", "y", "checked", "check", "on", "1", "x")
            await self._set_checked(field["ref"], want)
            return "checked" if want else "unchecked"
        if kind == "file":
            key = value.strip().lower().replace(" ", "_").replace("-", "_")
            path = self.files.get(key)
            if path is None:
                available = ", ".join(sorted(self.files)) or "none"
                raise ToolError(f"no file called {value!r} is available (available: {available})")
            await self._locator(field["ref"]).set_input_files(str(path), timeout=10000)
            await asyncio.sleep(1.5)  # many ATS upload the file immediately
            return f"attached {path.name}"
        if kind == "combobox":
            return await self._combobox(field, value)
        raise ToolError(f"don't know how to fill a {kind} field")

    def _match_option(self, field: dict, value: str, ref: str) -> dict:
        options = field.get("options") or []
        by_ref = next((o for o in options if o.get("ref") == ref), None)
        if by_ref is not None:
            return by_ref
        label = best_option(value.lstrip("!"), [o["label"] for o in options])
        if label is None:
            choices = " | ".join(o["label"] for o in options[:30])
            raise ToolError(f"no option matches {value!r}; options are: {choices}")
        return next(o for o in options if o["label"] == label)

    async def _set_checked(self, ref: str, checked: bool) -> None:
        from playwright.async_api import Error as PlaywrightError

        loc = self._locator(ref)
        role = await loc.get_attribute("role")
        if role in ("checkbox", "radio"):
            current = (await loc.get_attribute("aria-checked")) == "true"
            if current != checked:
                await loc.click(timeout=5000)
            return
        try:
            await loc.set_checked(checked, timeout=3000)
        except PlaywrightError:
            # Custom-styled inputs are often invisible; click their label instead.
            await loc.evaluate(
                """(el, want) => {
                    if (el.checked === want) return;
                    const target = (el.labels && el.labels[0]) || el;
                    target.click();
                }""",
                checked,
            )
        if await loc.evaluate("el => !!el.checked") != checked:
            raise ToolError("the checkbox/radio didn't change; try clicking its label or take a screenshot")

    async def _select(self, field: dict, value: str) -> str:
        loc = self._locator(field["ref"])
        options = field.get("options") or []
        if field.get("option_count", 0) > len(options):
            # Long lists were truncated in the snapshot; read them all now.
            options = await loc.evaluate(
                "el => Array.from(el.options).map(o => ({label: (o.label || o.text).trim(), value: o.value}))"
            )
        wanted = [v.strip() for v in value.split(";")] if field["kind"] == "multiselect" else [value]
        chosen = []
        for item in wanted:
            label = best_option(item, [o["label"] for o in options])
            if label is None:
                choices = " | ".join(o["label"] for o in options[:40])
                raise ToolError(f"no option matches {item!r}; options are: {choices}")
            chosen.append(next(o for o in options if o["label"] == label))
        await loc.select_option(value=[o["value"] for o in chosen], timeout=6000)
        return "selected " + ", ".join(repr(o["label"]) for o in chosen)

    async def _visible_options(self, frame: Any) -> list[tuple[str, Any]]:
        found = []
        for selector in ('[role="option"]', '[class*="option" i][id*="option"]', "li[class*='option' i]"):
            loc = frame.locator(f"{selector}:visible")
            count = min(await loc.count(), 80)
            for i in range(count):
                item = loc.nth(i)
                text = re.sub(r"\s+", " ", (await item.inner_text(timeout=2000)) or "").strip()
                if text:
                    found.append((text, item))
            if found:
                break
        return found

    async def _combobox(self, field: dict, value: str) -> str:
        loc = self._locator(field["ref"])
        frame = self.ref_frame[field["ref"]]
        await loc.click(timeout=6000)
        is_input = await loc.evaluate("el => el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'")
        if is_input:
            await loc.fill("", timeout=3000)
            await loc.press_sequentially(value[:80], delay=25, timeout=10000)
        options: list[tuple[str, Any]] = []
        for attempt in range(6):
            await asyncio.sleep(0.4)
            options = await self._visible_options(frame)
            if options:
                break
            if attempt == 2:
                await loc.click(timeout=3000)  # some dropdowns only open on a second click
        if not options:
            if is_input:
                await loc.press("Tab")
                return "typed the text (no suggestions appeared)"
            raise ToolError("no options appeared after opening the dropdown")
        label = best_option(value, [text for text, _ in options])
        if label is None:
            choices = " | ".join(text for text, _ in options[:25])
            await loc.press("Escape")
            raise ToolError(f"no option matches {value!r}; visible options: {choices}")
        await next(item for text, item in options if text == label).click(timeout=5000)
        await asyncio.sleep(0.3)
        return f"selected {label!r}"

    async def list_options(self, ref: str) -> str:
        field = self.fields.get(ref)
        if field is None:
            raise ToolError(f"unknown ref {ref}; call get_page_state to refresh")
        if field.get("options"):
            options = field["options"]
            if field.get("option_count", 0) > len(options):
                options = await self._locator(ref).evaluate(
                    "el => Array.from(el.options).map(o => ({label: (o.label || o.text).trim()}))"
                )
            return " | ".join(o["label"] for o in options if o["label"])
        if field["kind"] != "combobox":
            raise ToolError(f"field {ref} is a {field['kind']} field without options")
        loc = self._locator(ref)
        await loc.click(timeout=6000)
        await asyncio.sleep(0.6)
        options = await self._visible_options(self.ref_frame[ref])
        await loc.press("Escape")
        if not options:
            return "No options are listed until you type; fill the field with the text you want and the closest suggestion will be picked."
        return " | ".join(text for text, _ in options)

    async def click(self, ref: str) -> str:
        info = self.buttons.get(ref) or self.fields.get(ref)
        if info is None:
            raise ToolError(f"unknown ref {ref}; call get_page_state to refresh")
        if info.get("final_submit"):
            raise ToolError(
                "blocked: this looks like the button that submits the application. "
                + (
                    "Submission is disabled in review mode; call finish with status ready_for_review."
                    if self.mode == "review"
                    else "Use submit_application for the final submit."
                )
            )
        pages_before = len(self.context.pages)
        await self._locator(ref).click(timeout=8000)
        await self.settle(6000)
        note = " (it opened a new tab, which is now active)" if len(self.context.pages) > pages_before else ""
        return f"Clicked {info.get('label', '')[:60]!r}{note}. Now at: {self.page.url}. Call get_page_state to see the page."

    @property
    def submit_allowed(self) -> bool:
        return self.mode == "submit"

    async def submit(self, ref: str) -> str:
        if not self.submit_allowed:
            raise ToolError("submission is disabled in review mode; call finish with status ready_for_review")
        info = self.buttons.get(ref)
        if info is None:
            raise ToolError(f"unknown ref {ref}; call get_page_state to refresh")
        for frame in self.page.frames:
            with contextlib.suppress(Exception):
                await frame.evaluate("() => { window.__jobbotReview = false; }")
        url_before = self.page.url
        await self._locator(ref).click(timeout=10000)
        await self.settle(15000)
        await asyncio.sleep(1.5)
        state = await self.snapshot()
        body = state.get("text", "")
        confirmed = bool(CONFIRMATION_RE.search(body))
        if confirmed:
            self.submitted = True
        empty_required = [f for f in state["fields"] if f.get("required") and _is_empty(f)]
        lines = [
            f"Clicked submit. URL {'changed to ' + self.page.url if self.page.url != url_before else 'unchanged'}.",
            "The page shows a confirmation message." if confirmed else "No confirmation message detected.",
        ]
        if state["alerts"]:
            lines.append("Alerts: " + " | ".join(state["alerts"][:6]))
        if any("visible" in c for c in state["captcha"]):
            lines.append("A CAPTCHA challenge is now visible.")
        if empty_required:
            lines.append(
                "Required fields still empty: "
                + ", ".join(repr(f["label"][:50]) for f in empty_required[:10])
            )
        lines.append("Page text excerpt: " + body[:600])
        return "\n".join(lines)

    async def screenshot_block(self) -> dict:
        data = await self.page.screenshot(type="jpeg", quality=60, timeout=15000)
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(data).decode()},
        }

    async def save_screenshot(self, path: Path) -> bool:
        try:
            await self.page.screenshot(path=str(path), full_page=True, timeout=20000)
            return True
        except Exception as exc:
            log.warning("Screenshot failed: %s", exc)
            return False


def _is_empty(field: dict) -> bool:
    value = field.get("value")
    if field["kind"] == "checkbox":
        return not value
    return value in (None, "", [])


def answers_from_snapshot(state: dict) -> list[dict]:
    """The filled-in form as label/value pairs, for review in the UI."""
    answers = []
    for field in state.get("fields", []):
        value = field.get("value")
        if field["kind"] == "checkbox":
            value = "Checked" if value else "Not checked"
        label = re.sub(r"[\s*✱]+$", "", field.get("label") or "") or "(unlabeled field)"
        answers.append(
            {
                "label": label[:300],
                "value": str(value or "")[:2000],
                "kind": field["kind"],
                "required": bool(field.get("required")),
            }
        )
    return answers


def format_state(state: dict) -> str:
    """Render a snapshot as compact text for the model."""
    lines = [f"URL: {state.get('url', '')}", f"Title: {state.get('title', '')}"]
    if state.get("headings"):
        lines.append("Headings: " + " | ".join(state["headings"]))
    captcha = state.get("captcha") or []
    lines.append(f"CAPTCHA: {', '.join(captcha) if captcha else 'none detected'}")
    if state.get("login"):
        lines.append("Login/password form detected on this page.")
    fields = state.get("fields") or []
    lines.append("")
    lines.append(f"Form fields ({len(fields)}):" if fields else "Form fields: none on this page.")
    for f in fields:
        kind = f["kind"]
        req = " required" if f.get("required") else ""
        bad = " INVALID" if f.get("invalid") else ""
        label = (f.get("label") or "(no label)").replace("\n", " ")[:200]
        line = f"[{f['ref']}] {kind}{req}{bad} {label!r}"
        if kind in ("select", "multiselect"):
            opts = [o["label"] for o in f.get("options") or []]
            more = f.get("option_count", len(opts)) - len(opts)
            line += " options: " + " | ".join(opts[:25])
            if more > 0 or len(opts) > 25:
                line += f" … ({f.get('option_count', len(opts))} total; type the exact label)"
            line += f" — selected: {f.get('value') or '(none)'}"
        elif kind in ("radio", "checkboxes"):
            line += " options: " + " | ".join(o["label"] for o in f.get("options") or [])
            line += f" — {'selected' if kind == 'radio' else 'checked'}: {f.get('value') or '(none)'}"
        elif kind == "checkbox":
            line += " — " + ("checked" if f.get("value") else "unchecked")
        elif kind == "file":
            line += f" — {('attached: ' + f['value']) if f.get('value') else 'no file attached'}"
            if f.get("accept"):
                line += f" (accepts {f['accept'][:60]})"
        elif kind == "combobox":
            line += f" — value: {f.get('value')!r} (dropdown; fill picks the closest option)"
        else:
            line += f" — value: {str(f.get('value') or '')[:300]!r}"
            if f.get("placeholder"):
                line += f" (placeholder {f['placeholder']!r})"
        lines.append(line)
    buttons = state.get("buttons") or []
    if buttons:
        lines.append("")
        lines.append("Buttons and links:")
        for b in buttons:
            line = f"[{b['ref']}] {b['kind']} {b['label']!r}"
            if b.get("context"):
                line += f" (under question {b['context'][:120]!r})"
            if b.get("pressed"):
                line += " [selected]"
            if b.get("disabled"):
                line += " [disabled]"
            if b.get("final_submit"):
                line += " [FINAL SUBMIT]"
            lines.append(line)
    if state.get("alerts"):
        lines.append("")
        lines.append("Alerts: " + " | ".join(state["alerts"][:12]))
    if state.get("text"):
        lines.append("")
        lines.append("Page text (excerpt): " + state["text"][:1500])
    text = "\n".join(lines)
    if len(text) > MAX_STATE_CHARS:
        text = text[:MAX_STATE_CHARS] + "\n[page state truncated]"
    return text

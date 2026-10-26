"""Test doubles: a fake Anthropic client that replays scripted turns, and a rule-based "brain"
that drives the apply agent's tools like a model would."""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

_ids = itertools.count(1)


def tool_use(name: str, **inputs: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=inputs)


def text(value: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=value)


def response(*blocks: Any, stop_reason: str | None = None, parsed: Any = None) -> SimpleNamespace:
    has_tool = any(getattr(b, "type", "") == "tool_use" for b in blocks)
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason or ("tool_use" if has_tool else "end_turn"),
        stop_details=None,
        model="claude-opus-5-5",
        parsed_output=parsed,
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0
        ),
    )


class FakeMessages:
    def __init__(self, handler: Callable[[dict[str, Any]], Any]):
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.handler(kwargs)

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.handler(kwargs)


class FakeAnthropic:
    """Stands in for AsyncAnthropic: `client.beta.messages.create/parse` call `handler(kwargs)`."""

    def __init__(self, handler: Callable[[dict[str, Any]], Any]):
        self.messages = FakeMessages(handler)
        self.beta = SimpleNamespace(messages=self.messages)


def last_tool_result_text(kwargs: dict[str, Any]) -> str:
    """Text of the tool results in the latest user message."""
    content = kwargs["messages"][-1]["content"]
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            value = block.get("content")
            parts.append(value if isinstance(value, str) else "")
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block["text"])
    return "\n".join(parts)


FIELD_LINE = re.compile(
    r"^\[(?P<ref>[0-9]+g?)\] (?P<kind>\w+)(?P<req> required)?(?: INVALID)? '(?P<label>[^']*)'"
)
BUTTON_LINE = re.compile(r"^\[(?P<ref>\d+)\] (?:button|link) '(?P<label>[^']*)'(?P<rest>.*)$")


def parse_state(state: str) -> tuple[list[dict], list[dict]]:
    fields, buttons = [], []
    for line in state.splitlines():
        if m := BUTTON_LINE.match(line):
            buttons.append(m.groupdict())
        elif m := FIELD_LINE.match(line):
            fields.append(m.groupdict() | {"line": line})
    return fields, buttons


class FormBrain:
    """A rule-based stand-in for Claude that fills forms from a label->value table.

    It reads the page, clicks an "Apply" link if there is no form, fills everything it has an
    answer for, re-reads the page, then either submits (submit mode) or finishes for review.
    """

    def __init__(self, answers: dict[str, str], *, try_submit_in_review: bool = False):
        self.answers = answers
        self.try_submit_in_review = try_submit_in_review
        self.phase = "start"
        self.mode = "review"
        self.submit_ref: str | None = None
        self.log: list[str] = []

    def _value_for(self, label: str, kind: str) -> str | None:
        low = label.lower()
        for key, value in self.answers.items():
            if key.lower() in low:
                return value
        return None

    def __call__(self, kwargs: dict[str, Any]) -> Any:
        latest_text = last_tool_result_text(kwargs)
        if "submit mode" in latest_text.lower() or "Mode: submit" in latest_text:
            self.mode = "submit"
        if self.phase == "start":
            self.phase = "read"
            return response(tool_use("get_page_state"))
        if self.phase == "read":
            fields, buttons = parse_state(latest_text)
            if not fields:
                apply = next((b for b in buttons if "apply" in b["label"].lower()), None)
                if apply:
                    self.phase = "start"
                    return response(tool_use("click", ref=apply["ref"]))
                return response(
                    tool_use(
                        "finish", status="failed", summary="No form", missing_info=[], generated_answers=[]
                    )
                )
            if "CAPTCHA: reCAPTCHA (visible" in latest_text:
                return response(
                    tool_use(
                        "finish", status="blocked", summary="CAPTCHA", missing_info=[], generated_answers=[]
                    )
                )
            if "Login/password form detected" in latest_text:
                return response(
                    tool_use(
                        "finish",
                        status="blocked",
                        summary="Login required",
                        missing_info=[],
                        generated_answers=[],
                    )
                )
            actions = []
            for f in fields:
                value = self._value_for(f["label"], f["kind"])
                if value is not None:
                    actions.append({"ref": f["ref"], "value": value})
            submit = next((b for b in buttons if "[FINAL SUBMIT]" in b["rest"]), None)
            self.submit_ref = submit["ref"] if submit else None
            self.phase = "verify"
            # Answer Yes/No questions rendered as buttons.
            yes_no = [
                b
                for b in buttons
                if b["label"] in ("Yes", "No") and self._value_for(b["rest"], "button") == b["label"]
            ]
            calls = [tool_use("fill_fields", actions=actions)]
            calls += [tool_use("click", ref=b["ref"]) for b in yes_no]
            return response(*calls)
        if self.phase == "verify":
            self.phase = "check"
            return response(tool_use("get_page_state"))
        if self.phase == "check":
            self.log.append(latest_text)
            if self.mode == "submit" and self.submit_ref:
                self.phase = "submitted"
                return response(tool_use("submit_application", ref=self.submit_ref))
            if self.try_submit_in_review and self.submit_ref:
                self.phase = "tried_submit"
                return response(tool_use("click", ref=self.submit_ref))
            self.phase = "paused"
            return response(
                tool_use(
                    "finish",
                    status="ready_for_review",
                    summary="Filled in the form.",
                    missing_info=[],
                    generated_answers=["Why do you want to work at Acme?"],
                )
            )
        if self.phase == "tried_submit":
            self.log.append(latest_text)
            self.phase = "paused"
            return response(
                tool_use(
                    "finish",
                    status="ready_for_review",
                    summary="Filled.",
                    missing_info=[],
                    generated_answers=[],
                )
            )
        if self.phase == "paused":
            # Resumed after approval: re-read and submit.
            self.mode = "submit" if "submit mode" in latest_text.lower() else self.mode
            self.phase = "check"
            return response(tool_use("get_page_state"))
        if self.phase == "submitted":
            self.log.append(latest_text)
            self.phase = "done"
            return response(
                tool_use(
                    "finish", status="submitted", summary="Submitted.", missing_info=[], generated_answers=[]
                )
            )
        return response(text("done"))

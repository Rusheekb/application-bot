"""Claude integration, exercised through the real Anthropic SDK with a mocked HTTP transport so the
exact request bodies are checked."""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from jobbot.ai.client import FALLBACK_BETA, LLM, AIRefused, describe_error
from jobbot.ai.context import application_profile_block, candidate_block, job_block
from jobbot.ai.fit import analyze_fit, analyze_many
from jobbot.ai.resume import extract_profile, merge_extraction
from jobbot.ai.writing import draft_cover_letter
from jobbot.apply.agent import TOOLS, ApplyAgent, task_prompt
from jobbot.models import Job
from jobbot.schemas import AISettings, Education, Profile, QAPair, ResumeExtraction

FIT = {
    "score": 104,
    "interview_likelihood": "high",
    "summary": "Strong match.",
    "strengths": ["Python"],
    "gaps": [],
    "dealbreakers": [],
    "seniority_fit": "match",
    "tips": [],
}


def message(content: list[dict], stop_reason: str = "end_turn", **extra) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        **extra,
    }


class Recorder:
    """Mock transport handler recording requests and replaying queued responses."""

    def __init__(self, *responses: httpx2.Response):
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def make_llm(recorder: Recorder, **settings) -> LLM:
    client = AsyncAnthropic(
        api_key="test-key",
        base_url="http://anthropic.test",
        max_retries=0,
        http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(recorder)),
    )
    return LLM(AISettings(**settings), client=client)


JOB = Job(
    id=7,
    title="Backend Engineer",
    company="Acme",
    location="New York, NY",
    workplace="hybrid",
    description="Python and AWS. 3+ years.",
    source="greenhouse",
    url="https://example.test/job",
)
PROFILE = Profile(
    first_name="Jane",
    last_name="Doe",
    email="jane@example.com",
    phone="555-0100",
    current_title="Software Engineer",
    years_experience=4,
    skills=["Python", "AWS"],
    target_titles=["Backend Engineer"],
    education=[
        Education(
            school="State University", degree="BS", field_of_study="Computer Science", graduation_year="2021"
        )
    ],
    qa=[QAPair(question="Are you willing to work on-site 3 days a week?", answer="Yes")],
)


async def test_fit_request_shape_and_parsing():
    rec = Recorder(httpx2.Response(200, json=message([{"type": "text", "text": json.dumps(FIT)}])))
    llm = make_llm(rec)
    fit = await analyze_fit(llm, JOB, candidate_block(PROFILE, "Resume text"))
    assert fit.score == 100  # clamped
    body = rec.body()
    headers = rec.requests[-1].headers
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in headers["anthropic-beta"]
    assert "temperature" not in body
    assert "thinking" not in body  # adaptive by default on current models
    assert body["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert "jane@example.com" not in json.dumps(body["system"])
    assert "<job_posting>" in body["messages"][0]["content"]
    assert llm.usage.requests == 1


async def test_fallbacks_turn_off_when_unavailable():
    rec = Recorder(
        httpx2.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "fallbacks: not supported"},
            },
        ),
        httpx2.Response(200, json=message([{"type": "text", "text": json.dumps(FIT)}])),
    )
    llm = make_llm(rec)
    await analyze_fit(llm, JOB, "candidate")
    assert "fallbacks" in rec.body(0)
    assert "fallbacks" not in rec.body(1)
    assert "anthropic-beta" not in rec.requests[1].headers


async def test_fallbacks_only_for_supported_models():
    rec = Recorder(httpx2.Response(200, json=message([{"type": "text", "text": json.dumps(FIT)}])))
    await analyze_fit(make_llm(rec, model="claude-haiku-5-5"), JOB, "candidate")
    assert "fallbacks" not in rec.body()


async def test_refusal_raises():
    rec = Recorder(
        httpx2.Response(
            200,
            json=message(
                [],
                stop_reason="refusal",
                stop_details={"type": "refusal", "category": "cyber", "explanation": None},
            ),
        )
    )
    with pytest.raises(AIRefused, match="cyber"):
        await analyze_fit(make_llm(rec), JOB, "candidate")


async def test_analyze_many_reports_each_job():
    rec = Recorder(httpx2.Response(200, json=message([{"type": "text", "text": json.dumps(FIT)}])))
    llm = make_llm(rec)
    results = []
    jobs = [
        JOB,
        Job(id=8, title="Data Engineer", company="X", description="", source="lever", url="https://x.test"),
    ]
    await analyze_many(llm, jobs, "candidate", lambda job, fit, exc: results.append((job.id, fit.score, exc)))
    assert sorted(r[0] for r in results) == [7, 8]
    assert all(r[2] is None for r in results)


async def test_resume_pdf_sent_as_document(tmp_path: Path):
    extraction = ResumeExtraction(
        first_name="Jane", last_name="Doe", email="jane@example.com", phone="", city="Austin", state="TX",
        country="US", linkedin_url="", github_url="https://github.com/jane", portfolio_url="",
        current_title="Data Engineer", current_company="Globex", years_experience=4.5,
        skills=["Python", "Spark", "python"], summary="Data engineer.", education=[], certifications=[],
        languages=["English"], suggested_target_titles=["Data Engineer", "Senior Data Engineer"],
    )  # fmt: skip
    rec = Recorder(
        httpx2.Response(200, json=message([{"type": "text", "text": extraction.model_dump_json()}]))
    )
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    result = await extract_profile(make_llm(rec), pdf, "")
    content = rec.body()["messages"][0]["content"]
    assert content[0]["type"] == "document" and content[0]["source"]["media_type"] == "application/pdf"

    profile = Profile(first_name="Janet", skills=["Python"])
    changed = merge_extraction(profile, result)
    assert profile.first_name == "Janet"  # never overwrites what you typed
    assert profile.last_name == "Doe" and profile.years_experience == 4.5
    assert profile.skills == ["Python", "Spark"]
    assert profile.target_titles == ["Data Engineer", "Senior Data Engineer"]
    assert "first_name" not in changed and "skills" in changed


async def test_cover_letter_uses_candidate_without_contact_details():
    rec = Recorder(httpx2.Response(200, json=message([{"type": "text", "text": "Dear team, ..."}])))
    letter = await draft_cover_letter(make_llm(rec), JOB, PROFILE, "Resume text")
    assert letter == "Dear team, ..."
    system = json.dumps(rec.body()["system"])
    assert "Jane Doe" in system and "555-0100" not in system


def test_context_blocks():
    candidate = candidate_block(PROFILE, "RESUME")
    assert "Needs visa sponsorship: no" in candidate and "jane@example.com" not in candidate
    assert "BS in Computer Science, State University (2021)" in candidate
    application = application_profile_block(PROFILE, "RESUME")
    assert "Email: jane@example.com" in application
    assert "Gender: (blank: decline to self-identify)" in application
    assert "Q: Are you willing to work on-site 3 days a week?\nA: Yes" in application
    assert "Workplace: hybrid" in job_block(JOB)
    # Deterministic output keeps prompt caching effective.
    assert candidate_block(PROFILE, "RESUME") == candidate


def test_tools_are_strict_and_well_formed():
    names = [t["name"] for t in TOOLS]
    assert names == [
        "get_page_state",
        "fill_fields",
        "click",
        "list_options",
        "screenshot",
        "submit_application",
        "finish",
    ]
    for tool in TOOLS:
        assert tool["strict"] is True
        schema = tool["input_schema"]
        assert schema["additionalProperties"] is False and set(schema["required"]) <= set(
            schema["properties"]
        )


def test_task_prompt_modes():
    review = task_prompt(
        JOB, mode="review", start_url="https://x.test/apply", allow_consent=True, cover_letter_file=False
    )
    assert "Mode: review" in review and "do not submit" in review
    submit = task_prompt(
        JOB, mode="submit", start_url="https://x.test/apply", allow_consent=False, cover_letter_file=True,
        previous_answers=[{"label": "Email", "value": "jane@example.com"}],
    )  # fmt: skip
    assert "Mode: submit" in submit and "don't tick privacy" in submit
    assert "- Email: jane@example.com" in submit


async def test_agent_requests_keep_a_stable_cacheable_prefix():
    """Each agent turn sends the same tools/system with summarized thinking and auto-caching, and
    appends to the conversation instead of editing it."""
    rec = Recorder(
        httpx2.Response(200, json=message([
            {"type": "thinking", "thinking": "Let me look.", "signature": "sig"},
            {"type": "tool_use", "id": "toolu_1", "name": "finish", "input": {"status": "failed", "summary": "No form here.", "missing_info": [], "generated_answers": []}},
        ], stop_reason="tool_use")),
    )  # fmt: skip

    class NoBrowser:
        mode = "review"
        submit_attempts = 0

    steps = []
    agent = ApplyAgent(
        make_llm(rec), NoBrowser(), profile_text="<candidate>Jane</candidate>", task="Apply.", mode="review",
        effort="medium", max_steps=10, on_step=steps.append,
    )  # fmt: skip
    outcome = await agent.start()
    assert outcome.status == "failed" and outcome.summary == "No form here."
    body = rec.body()
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"] == {"effort": "medium"}
    assert [t["name"] for t in body["tools"]] == [t["name"] for t in TOOLS]
    assert body["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert any(s["kind"] == "thinking" and s["text"] == "Let me look." for s in steps)


def test_describe_error_messages():
    assert describe_error(AIRefused("Claude declined this request")) == "Claude declined this request"
    assert "ValueError" in describe_error(ValueError("x"))

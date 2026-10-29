"""The hosted board in a real browser: each visitor's settings stay in their browser, and Claude
runs with the visitor's own key (the Anthropic API is stubbed here)."""

from __future__ import annotations

import functools
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest

from jobbot.publish import write_site
from tests.conftest import _QuietHandler, requires_browser

KEY = "sk-ant-api03-" + "t3st" * 10
CORS = {"Access-Control-Allow-Origin": "*"}
RESUME = "Jane Doe. Backend engineer at Initech, 2023-2026. Python, SQL, Kafka."


def iso(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def job(id: str, title: str, company: str, **extra) -> dict:
    return {
        "id": id,
        "title": title,
        "company": company,
        "location": "New York, NY",
        "workplace": "",
        "type": "",
        "salary": "",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "salary_period": None,
        "posted": iso(timedelta(days=2)),
        "found": iso(timedelta(days=1)),
        "years": None,
        "seniority": None,
        "no_sponsorship": False,
        "clearance": False,
        "url": f"https://jobs.example.com/{id}",
        "apply": f"https://jobs.example.com/{id}/apply",
        "sources": ["Greenhouse"],
        "description": "Build APIs in Python and SQL.",
        **extra,
    }


def sample_feed() -> dict:
    return {
        "version": 1,
        "generated": iso(timedelta(hours=1)),
        "repository": "someone/job-search",
        "scan": {
            "status": "partial",
            "started": iso(timedelta(hours=1)),
            "stats": {},
            "errors": [{"source": "Search: AI Engineer · Texas (LinkedIn)", "error": "HTTP 429"}],
        },
        "sources": {"companies": 4, "searches": ["AI Engineer · Texas"]},
        "jobs": [
            job("a1", "Backend Engineer", "Acme", years=3, salary="$140k–$170k"),
            job(
                "b2",
                '<img src=x onerror="window.pwned=1">Data Engineer',
                "Globex",
                url="javascript:window.pwned=1",
                apply=None,
            ),
            job("c3", "Staff Backend Engineer", "Initech", years=10, seniority="staff"),
            job("d4", "Account Executive", "Acme"),
            job("e5", "Backend Engineer, Payments", "Umbrella", location="Remote - US", workplace="remote"),
        ],
    }


@pytest.fixture
def board_site(tmp_path):
    site = tmp_path / "site"
    write_site(site, sample_feed())
    (site / "frame.html").write_text('<!doctype html><iframe src="index.html"></iframe>')
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=str(site)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


def watch(page) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {"errors": [], "csp": [], "requests": []}
    page.on("pageerror", lambda exc: seen["errors"].append(str(exc)))
    page.on("console", lambda m: seen["csp"].append(m.text) if "Content Security Policy" in m.text else None)
    page.on("request", lambda r: seen["requests"].append(r.url))
    return seen


def storage(page, kind: str) -> str:
    return page.evaluate(f"JSON.stringify({kind})")


@requires_browser
def test_each_visitor_tailors_the_board_in_their_own_browser(board_site):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        seen = watch(page)
        page.goto(board_site)
        # Saved marks from the earlier passphrase board carry over; its stored key is deleted.
        page.evaluate(
            """() => {
              localStorage.setItem('jobbot:status:-x-', JSON.stringify({a1: 'saved'}));
              localStorage.setItem('jobbot:key:-x-', JSON.stringify('old key'));
            }"""
        )
        page.reload()

        # A first visit opens Settings, filled in with the defaults.
        expect(page.locator("#welcome")).to_contain_text("5 current jobs")
        field = lambda path: page.locator(f'[data-path="{path}"]')  # noqa: E731
        expect(field("settings.filters.max_years_gap")).to_have_value("2")
        expect(field("settings.filters.include_remote")).to_be_checked()
        field("profile.target_titles").fill("Backend Engineer\nData Engineer")
        field("profile.years_experience").fill("3")
        field("profile.skills").fill("Python\nSQL")
        page.click("#save-settings")
        expect(page.locator("#settings-message")).to_contain_text("Saved. 3 jobs match your settings")
        page.get_by_role("button", name="See them").click()

        # The board ranks jobs against those settings, with the reasons for what it hides.
        expect(page.locator("#subtitle")).to_contain_text(
            "1 other job in the feed doesn't match your target titles"
        )
        expect(page.locator(".job")).to_have_count(2)  # the Backend Engineer job is already saved
        # An exact title match ranks first. Untrusted text stays text, and only http(s) links are linked.
        expect(page.locator(".job .title")).to_have_text(
            ['<img src=x onerror="window.pwned=1">Data Engineer', "Backend Engineer, Payments"]
        )
        expect(page.locator(".job").first.locator("[data-action=open]")).to_have_count(0)
        assert page.evaluate("window.pwned") is None
        page.locator(".tabs button", has_text="Saved").click()
        expect(page.locator(".job .title")).to_have_text("Backend Engineer")
        expect(page.locator(".job .score")).to_contain_text("est.")
        page.locator(".tabs button", has_text="Hidden by filters").click()
        expect(page.locator(".job")).to_contain_text("Asks for 10+ years (you have 3)")
        expect(page.locator("#errors")).to_contain_text("1 source had problems in the last update")
        expect(page.locator("#ai-bar")).to_have_text("")
        page.locator(".tabs button", has_text="To review").click()
        expect(page.locator("#ai-bar")).to_contain_text("add your own Anthropic API key")
        assert page.evaluate("Object.keys(localStorage).filter((k) => k.startsWith('jobbot:'))") == []

        # It's all remembered in this browser, and only in this browser.
        page.locator(".job").first.locator("[data-action=dismissed]").click()
        page.reload()
        expect(page.locator("#jobs-view")).to_be_visible()
        expect(page.locator("#welcome")).to_have_count(0)
        expect(page.locator(".job")).to_have_count(1)
        saved = page.evaluate("JSON.parse(localStorage.getItem('jobbot.settings'))")
        assert saved["profile"]["target_titles"] == ["Backend Engineer", "Data Engineer"]
        other = browser.new_page()  # another visitor
        other.goto(board_site)
        expect(other.locator("#welcome")).to_be_visible()

        origin = board_site.rstrip("/")
        assert all(url.startswith(origin) for url in seen["requests"]), seen["requests"]
        assert seen["errors"] == [] and seen["csp"] == []
        browser.close()


class FakeAnthropic:
    """Stands in for api.anthropic.com."""

    def __init__(self):
        self.calls: list[dict] = []
        self.replies: list[tuple[int, dict]] = []  # canned answers for the next requests

    def __call__(self, route) -> None:
        request = route.request
        body = json.loads(request.post_data) if request.post_data else None
        self.calls.append(
            {"method": request.method, "url": request.url, "headers": request.headers, "body": body}
        )
        if self.replies:
            status, reply = self.replies.pop(0)
            route.fulfill(status=status, json=reply, headers=CORS)
        elif request.url.endswith("/count_tokens"):
            route.fulfill(json={"input_tokens": 8}, headers=CORS)
        else:
            if isinstance(body["system"], str):  # reading a resume
                answer = {
                    "current_title": "Backend Engineer",
                    "years_experience": 3,
                    "skills": ["Python", "SQL", "Kafka"],
                    "target_titles": ["Backend Engineer", "Platform Engineer"],
                    "summary": "Backend engineer with three years of experience.",
                }
            else:
                answer = {
                    "score": 74,
                    "interview_likelihood": "medium",
                    "summary": "Strong backend match.",
                    "strengths": ["Python APIs"],
                    "gaps": ["No payments experience"],
                    "dealbreakers": [],
                    "seniority_fit": "match",
                    "tips": ["Lead with your API work"],
                }
            route.fulfill(
                json={
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [{"type": "text", "text": json.dumps(answer)}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1200, "output_tokens": 300},
                },
                headers=CORS,
            )

    def posts(self) -> list[dict]:
        return [c for c in self.calls if c["url"].endswith("/v1/messages")]


@requires_browser
def test_claude_uses_the_visitors_own_key_and_keeps_it_safe(board_site):
    from playwright.sync_api import expect, sync_playwright

    anthropic = FakeAnthropic()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        seen = watch(page)
        page.route("https://api.anthropic.com/**", anthropic)
        page.goto(board_site)
        field = lambda path: page.locator(f'[data-path="{path}"]')  # noqa: E731
        message = page.locator("#settings-message")

        field("resume_text").fill(RESUME)
        page.click("#fill-from-resume")
        expect(message).to_contain_text("add your Anthropic API key")
        page.fill("#anthropic-key", "my-password-123")
        page.click("#save-settings")
        expect(message).to_contain_text("doesn't look like an Anthropic API key")
        assert "jobbot.settings" not in storage(page, "localStorage")  # nothing was saved
        assert "my-password" not in storage(page, "localStorage") + storage(page, "sessionStorage")

        page.fill("#anthropic-key", KEY)
        page.click("#test-key")
        expect(message).to_have_text("The key works.")
        page.click("#fill-from-resume")
        expect(message).to_contain_text(
            "Filled in target titles, years of experience, current title, 3 skills"
        )
        expect(field("profile.target_titles")).to_have_value("Backend Engineer\nPlatform Engineer")
        expect(field("profile.years_experience")).to_have_value("3")

        # Requests go straight to Anthropic with the key, and nothing else of the visitor's.
        test, read = anthropic.calls[0], anthropic.calls[1]
        assert test["url"] == "https://api.anthropic.com/v1/messages/count_tokens"  # free
        assert test["body"]["model"] == "claude-haiku-5-5"
        for call in (test, read):
            headers = call["headers"]
            assert headers["x-api-key"] == KEY
            assert headers["anthropic-version"] == "2026-10-21"
            assert headers["anthropic-dangerous-direct-browser-access"] == "true"
            assert "cookie" not in headers and "referer" not in headers
        assert read["body"]["model"] == "claude-haiku-5-5"
        assert read["body"]["output_config"]["format"]["type"] == "json_schema"
        assert RESUME in read["body"]["messages"][0]["content"]

        # By default the key is kept for this tab only, and it's never in a backup.
        page.click("#save-settings")
        expect(message).to_contain_text("Saved.")
        expect(page.locator("#key-status")).to_contain_text("for this tab only")
        assert KEY not in storage(page, "localStorage") and KEY in storage(page, "sessionStorage")
        page.get_by_role("button", name="Show these settings as JSON").click()
        backup = page.locator("#settings-json").input_value()
        assert KEY not in backup and "Platform Engineer" in backup
        tab = context.new_page()  # same browser, another tab: settings yes, key no
        tab.goto(board_site)
        expect(tab.locator("#ai-bar")).to_contain_text("add your own Anthropic API key")
        tab.close()

        # Claude reads a posting against the resume.
        page.get_by_role("button", name="Back to jobs").click()
        page.locator(".job", has_text="Acme").locator(".title").click()
        page.locator("[data-action=analyze]").click()
        card = page.locator(".job", has_text="Acme")
        expect(card.locator(".score")).to_contain_text("74")
        expect(card.locator(".score")).to_contain_text("AI")
        expect(card).to_contain_text("Decent chance. Strong backend match.")
        expect(card).to_contain_text("No payments experience")
        fit = anthropic.posts()[-1]["body"]
        candidate = fit["system"][1]
        assert candidate["cache_control"] == {"type": "ephemeral"}
        assert (
            RESUME in candidate["text"]
            and "Target roles: Backend Engineer, Platform Engineer" in candidate["text"]
        )
        assert (
            "<job_posting>" in fit["messages"][0]["content"]
            and "Title: Backend Engineer" in fit["messages"][0]["content"]
        )
        assert '"a1"' in page.evaluate("localStorage.getItem('jobbot.fits')")

        # Several at once, after saying what it sends and roughly what it costs.
        page.locator("[data-action=batch]").click()
        expect(page.locator("#ai-bar")).to_contain_text("billed to your key")
        before = len(anthropic.posts())
        page.locator("[data-action=confirm-batch]").click()
        # The other Backend Engineer job; Data Engineer isn't one of this visitor's titles.
        expect(page.locator("#ai-bar")).to_contain_text("Claude analyzed 1 job (less than a cent)")
        assert len(anthropic.posts()) == before + 1

        # Anthropic's errors are explained.
        anthropic.replies.append(
            (
                401,
                {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}},
            )
        )
        page.locator(".job", has_text="Acme").locator("[data-action=analyze]").click()
        expect(page.locator(".job", has_text="Acme")).to_contain_text("Anthropic didn't accept this API key")

        # Remembering it is a choice, and removing it removes it everywhere.
        page.click("#settings-button")
        page.check("#remember-key")
        page.click("#save-settings")
        expect(page.locator("#key-status")).to_contain_text("saved on this device")
        assert KEY in storage(page, "localStorage") and KEY not in storage(page, "sessionStorage")
        page.click("#remove-key")
        expect(page.locator("#key-status")).to_contain_text("No key saved")
        assert KEY not in storage(page, "localStorage") + storage(page, "sessionStorage")

        assert {c["url"].split("/v1/")[0] for c in anthropic.calls} == {"https://api.anthropic.com"}
        assert seen["errors"] == [] and seen["csp"] == []
        browser.close()


@requires_browser
def test_the_board_refuses_to_run_inside_another_page(board_site):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(board_site + "frame.html")
        frame = page.frame_locator("iframe")
        expect(frame.locator("#framed")).to_be_visible()
        expect(frame.locator("#app")).to_be_hidden()
        browser.close()

"""The hosted job board: config sync, encryption, the scheduled publish run and the static page."""

from __future__ import annotations

import base64
import functools
import gzip
import json
import re
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import respx
from sqlalchemy import select

from jobbot.db import Database, get_settings
from jobbot.models import Company, Job, SearchQuery
from jobbot.publish import (
    PublishError,
    board_payload,
    decode_config,
    decrypt,
    derive_key,
    encode_config,
    export_config,
    import_config,
    publish,
    restore_state,
    save_state,
    site_salt,
    write_site,
)
from tests.conftest import _QuietHandler, load_json, requires_browser

PASSPHRASE = "correct horse battery staple"
CONFIG = {
    "profile": {
        "first_name": "Jane",
        "email": "jane@example.com",
        "years_experience": 3,
        "target_titles": ["Backend Engineer", "Data Engineer", "Software Engineer"],
        "skills": ["Python", "SQL", "Spark"],
    },
    "settings": {
        "filters": {"max_age_days": None},
        # Auto-apply can't run in the hosted board, whatever the config says.
        "apply": {"enabled": True, "auto_after_scan": True},
    },
    "companies": [
        "https://boards.greenhouse.io/acme",
        {"name": "Globex Corp", "url": "https://jobs.lever.co/globex"},
    ],
    "searches": [
        {"keywords": "backend engineer", "location": "New York", "sources": ["remotive", "myspace"]}
    ],
    "resume_text": "Jane Doe, backend engineer. Python, SQL.",
}


def read_envelope(index: Path) -> dict:
    match = re.search(
        r'<script id="jobbot-envelope" type="application/json">(.*?)</script>', index.read_text(), re.S
    )
    assert match, "no envelope in the page"
    return json.loads(match.group(1))


def open_envelope(envelope: dict, passphrase: str) -> dict:
    key = derive_key(passphrase, base64.b64decode(envelope["salt"]), envelope["iterations"])
    return json.loads(gzip.decompress(decrypt(envelope, key)))


def test_config_encoding_round_trip():
    encoded = encode_config(CONFIG)
    assert encoded.startswith("jobbot1:")
    assert decode_config(encoded) == CONFIG
    assert decode_config(json.dumps(CONFIG)) == CONFIG  # plain JSON works too
    with pytest.raises(PublishError):
        decode_config("jobbot1:not base64!")
    with pytest.raises(PublishError):
        decode_config("[1, 2]")


async def test_import_config_syncs_the_database(db):
    warnings = await import_config(db, CONFIG)
    assert len(warnings) == 1 and "myspace" in warnings[0]
    with db.session() as s:
        companies = {(c.ats, c.slug): c.name for c in s.scalars(select(Company))}
        assert companies == {("greenhouse", "acme"): "Acme", ("lever", "globex"): "Globex Corp"}
        search = s.scalars(select(SearchQuery)).one()
        assert (search.keywords, search.location, search.sources) == (
            "backend engineer",
            "New York",
            ["remotive"],
        )
        assert get_settings(s).apply.enabled is False

    exported = export_config(db)
    assert exported["profile"]["target_titles"] == CONFIG["profile"]["target_titles"]
    assert exported["resume_text"] == CONFIG["resume_text"]
    assert "apply" not in exported["settings"]
    assert {c["slug"] for c in exported["companies"]} == {"acme", "globex"}

    # Importing an edited config removes what's no longer listed, and keeps what is.
    edited = {
        **exported,
        "companies": [c for c in exported["companies"] if c["slug"] == "globex"],
        "searches": [],
    }
    assert await import_config(db, edited) == []
    with db.session() as s:
        assert [c.slug for c in s.scalars(select(Company))] == ["globex"]
        assert list(s.scalars(select(SearchQuery))) == []


def test_state_is_encrypted_and_restores(tmp_path, db):
    with db.session() as s:
        s.add(
            Job(
                fingerprint="f" * 40,
                source="lever",
                url="https://jobs.lever.co/x/1",
                title="Backend Engineer",
                company="Globex",
            )
        )
    key = derive_key(PASSPHRASE, site_salt("someone/jobs"), iterations=1000)
    state = tmp_path / "state" / "state.json"
    db.engine.dispose()
    save_state(db.path, state, key)
    assert b"Backend Engineer" not in state.read_bytes()

    restored = tmp_path / "restored" / "jobbot.db"
    assert restore_state(state, restored, key) == "restored"
    with Database(restored).session() as s:
        assert s.scalars(select(Job.title)).all() == ["Backend Engineer"]
    wrong = derive_key("another passphrase!", site_salt("someone/jobs"), iterations=1000)
    assert restore_state(state, tmp_path / "other.db", wrong) == "unreadable"
    assert restore_state(tmp_path / "missing.json", tmp_path / "other.db", key) == "missing"


async def test_publish_without_secrets_shows_setup_instructions(tmp_path, env, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/job-search")
    assert await publish(env, tmp_path / "site", tmp_path / "state.json", None, None) == 0
    page = (tmp_path / "site" / "index.html").read_text()
    assert "isn't set up yet" in page
    assert "https://github.com/someone/job-search/settings/secrets/actions" in page
    assert "__REPO_URL__" not in page
    assert not (tmp_path / "state.json").exists()


async def test_publish_rejects_short_passphrases(tmp_path, env):
    with pytest.raises(PublishError, match="at least 12 characters"):
        await publish(env, tmp_path / "site", tmp_path / "state.json", encode_config(CONFIG), "hunter2")


@respx.mock
async def test_publish_scans_and_writes_an_encrypted_board(tmp_path, env, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/job-search")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").respond(
        json=load_json("greenhouse_jobs.json")
    )
    respx.get("https://api.lever.co/v0/postings/globex").respond(json=load_json("lever_postings.json"))
    respx.get(url__startswith="https://remotive.com/api/remote-jobs").respond(json={"jobs": []})
    site, state = tmp_path / "site", tmp_path / "state" / "state.json"

    assert await publish(env, site, state, encode_config(CONFIG), PASSPHRASE) == 0

    index = (site / "index.html").read_text()
    for private in ("Backend Engineer", "Acme", "Jane", "jane@example.com", "Python"):
        assert private not in index
        assert private not in state.read_text()
    payload = open_envelope(read_envelope(site / "index.html"), PASSPHRASE)
    titles = {j["title"] for j in payload["jobs"]}
    assert {"Backend Engineer", "Data Engineer"} <= titles
    assert "Account Executive" not in titles  # not a target title
    hidden = {j["title"]: j["reason"] for j in payload["hidden"]}
    assert hidden["Senior Staff Software Engineer, Infrastructure"] == "Asks for 10+ years (you have 3)"
    assert payload["scan"]["status"] == "ok"
    assert any("myspace" in e["error"] for e in payload["scan"]["errors"])

    # The public log only has counts.
    log = capsys.readouterr().out
    assert "No saved job history yet" in log and "Published the encrypted board." in log
    for private in ("Backend", "Acme", "Globex", "backend engineer", "New York", "myspace"):
        assert private not in log

    # The next run picks up where this one left off.
    assert await publish(env, site, state, encode_config(CONFIG), PASSPHRASE) == 0
    assert "Restored the job history" in capsys.readouterr().out
    payload = open_envelope(read_envelope(site / "index.html"), PASSPHRASE)
    assert payload["scan"]["stats"]["new"] == 0
    assert {"Backend Engineer", "Data Engineer"} <= {j["title"] for j in payload["jobs"]}

    # Changing the passphrase starts a new history instead of failing.
    assert await publish(env, site, state, encode_config(CONFIG), "a different passphrase") == 0
    assert "Couldn't decrypt the saved job history" in capsys.readouterr().out


def sample_payload() -> dict:
    job = {
        "id": "a1",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "New York, NY",
        "workplace": "hybrid",
        "type": "",
        "salary": "$140k–$170k",
        "posted": "2026-10-20T12:00:00Z",
        "found": "2026-10-21T12:00:00Z",
        "score": 82,
        "ai": False,
        "likelihood": None,
        "summary": "",
        "strengths": [],
        "gaps": [],
        "dealbreakers": [],
        "tips": [],
        "seniority": None,
        "skills": ["Python", "SQL"],
        "notes": [],
        "breakdown": {"title": 30, "skills": 25, "experience": 15, "freshness": 9, "workplace": 3},
        "years": 3,
        "url": "https://job-boards.greenhouse.io/acme/jobs/1",
        "apply": "https://job-boards.greenhouse.io/acme/jobs/1",
        "sources": ["Greenhouse"],
        "description": "Build <b>APIs</b> in Python.",
    }
    sneaky = {
        **job,
        "id": "b2",
        "title": '<img src=x onerror="window.pwned=1">Data Engineer',
        "company": "Globex",
        "score": 55,
        "url": "javascript:window.pwned=1",
        "apply": None,
    }
    return {
        "generated": "2026-10-26T06:00:00Z",
        "scan": {
            "status": "partial",
            "started": "2026-10-26T05:59:00Z",
            "stats": {},
            "errors": [{"source": "LinkedIn", "error": "HTTP 429"}],
        },
        "interval_hours": 6,
        "jobs": [job, sneaky],
        "hidden": [
            {
                "id": "c3",
                "title": "Staff Engineer",
                "company": "Initech",
                "location": "",
                "reason": "Asks for 10+ years (you have 3)",
                "url": None,
            }
        ],
    }


@pytest.fixture
def board_site(tmp_path):
    salt = site_salt("someone/job-search")
    write_site(tmp_path / "site", sample_payload(), derive_key(PASSPHRASE, salt), salt)
    handler = functools.partial(_QuietHandler, directory=str(tmp_path / "site"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


@requires_browser
def test_board_page_decrypts_in_the_browser(board_site):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(board_site)
        expect(page.locator("#lock")).to_be_visible()
        assert "Backend Engineer" not in page.content()

        page.fill("#passphrase", "not the passphrase")
        page.click("#unlock-button")
        expect(page.locator("#unlock-error")).to_have_text("That passphrase didn't work.")

        page.fill("#passphrase", PASSPHRASE)
        page.check("#remember")
        page.click("#unlock-button")
        expect(page.locator("#app")).to_be_visible()
        expect(page.locator(".job")).to_have_count(2)
        first = page.locator(".job").first
        expect(first.locator(".title")).to_have_text("Backend Engineer")
        expect(first.locator(".score")).to_contain_text("82")
        # Untrusted text stays text, and only http(s) links are linked.
        expect(page.locator(".job").nth(1).locator(".title")).to_have_text(
            '<img src=x onerror="window.pwned=1">Data Engineer'
        )
        expect(page.locator(".job").nth(1).locator("[data-action=open]")).to_have_count(0)
        assert page.evaluate("window.pwned") is None
        expect(page.locator("#errors")).to_contain_text("1 problem in the last scan")

        # Triage is remembered in this browser.
        first.locator("[data-action=saved]").click()
        expect(page.locator(".job")).to_have_count(1)
        page.locator(".tabs button", has_text="Saved").click()
        expect(page.locator(".job .title")).to_have_text("Backend Engineer")
        page.locator(".tabs button", has_text="Hidden by filters").click()
        expect(page.locator(".job")).to_contain_text("Asks for 10+ years")

        # "Remember on this device" unlocks on the next visit, until Lock is pressed.
        page.reload()
        expect(page.locator("#app")).to_be_visible()
        page.locator(".tabs button", has_text="Saved").click()
        expect(page.locator(".job .title")).to_have_text("Backend Engineer")
        page.click("#lock-button")
        expect(page.locator("#lock")).to_be_visible()
        assert errors == []
        browser.close()


def test_board_payload_skips_closed_and_stale_jobs(db):
    from datetime import timedelta

    from jobbot.models import utcnow

    now = utcnow()
    with db.session() as s:
        base = {"source": "lever", "company": "Globex", "score": 70}
        s.add(Job(fingerprint="a" * 40, url="https://x.test/1", title="Current", last_seen_at=now, **base))
        s.add(
            Job(
                fingerprint="b" * 40,
                url="https://x.test/2",
                title="Closed",
                last_seen_at=now,
                closed_at=now,
                **base,
            )
        )
        s.add(
            Job(
                fingerprint="c" * 40,
                url="https://x.test/3",
                title="Stale",
                last_seen_at=now - timedelta(days=45),
                **base,
            )
        )
        s.add(
            Job(fingerprint="d" * 40, url="javascript:alert(1)", title="Bad link", last_seen_at=now, **base)
        )
    payload = board_payload(db)
    jobs = {j["title"]: j for j in payload["jobs"]}
    assert set(jobs) == {"Current", "Bad link"}
    assert jobs["Bad link"]["url"] is None


async def test_example_config_is_valid(db):
    example = Path(__file__).parents[1] / "examples" / "jobbot-config.example.json"
    config = decode_config(example.read_text())
    assert await import_config(db, config) == []
    with db.session() as s:
        assert {c.ats for c in s.scalars(select(Company))} == {"greenhouse", "lever", "ashby"}
        assert len(s.scalars(select(SearchQuery)).all()) == 2

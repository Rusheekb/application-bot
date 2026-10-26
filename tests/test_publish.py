"""The hosted job board: config sync, encryption, the scheduled publish run and the static page."""

from __future__ import annotations

import base64
import functools
import gzip
import json
import re
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import respx
from sqlalchemy import select

from jobbot.db import Database, get_settings
from jobbot.models import Company, Job, SearchQuery
from jobbot.publish import (
    CONFIG_FILE,
    PublishError,
    board_payload,
    board_settings,
    decode_config,
    decrypt,
    derive_key,
    encode_config,
    encrypt_config,
    export_config,
    git_blob_sha,
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
def serve_board(tmp_path):
    """Publish a board page with the given payload and serve it on localhost."""
    servers = []

    def serve(payload: dict) -> str:
        site = tmp_path / f"site{len(servers)}"
        salt = site_salt("someone/job-search")
        write_site(site, payload, derive_key(PASSPHRASE, salt), salt)
        server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=str(site)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/"

    yield serve
    for server in servers:
        server.shutdown()


@pytest.fixture
def board_site(serve_board):
    return serve_board(sample_payload())


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


# -- settings edited on the board ------------------------------------------------------------


def mock_sources() -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").respond(
        json=load_json("greenhouse_jobs.json")
    )
    respx.get("https://api.lever.co/v0/postings/globex").respond(json=load_json("lever_postings.json"))
    respx.get(url__startswith="https://remotive.com/api/remote-jobs").respond(json={"jobs": []})


def saved_settings(path: Path, config: dict, passphrase: str = PASSPHRASE) -> Path:
    """Write a settings file the way the board's Settings page saves it."""
    salt = site_salt("someone/job-search")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encrypt_config(config, derive_key(passphrase, salt), salt))
    return path


@respx.mock
async def test_settings_saved_from_the_board_are_used(tmp_path, env, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/job-search")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    mock_sources()
    saved = saved_settings(tmp_path / "repo" / CONFIG_FILE, CONFIG)
    # Settings saved on the board win over the JOBBOT_CONFIG secret.
    secret = encode_config({**CONFIG, "searches": [{"keywords": "from the secret", "sources": ["remotive"]}]})
    site, state = tmp_path / "site", tmp_path / "state.json"

    assert await publish(env, site, state, secret, PASSPHRASE, config_file=saved) == 0

    for private in ("Backend Engineer", "jane@example.com", "Python"):
        assert private not in saved.read_text()
    payload = open_envelope(read_envelope(site / "index.html"), PASSPHRASE)
    assert payload["scan"]["status"] == "ok"
    assert {"Backend Engineer", "Data Engineer"} <= {j["title"] for j in payload["jobs"]}
    meta = payload["settings"]
    assert meta["config"] == CONFIG and meta["origin"] == "board"
    assert (meta["repository"], meta["path"]) == ("someone/job-search", ".jobbot/config.enc.json")
    assert not meta["needs_setup"] and not meta["unreadable"]
    assert meta["sha"] == git_blob_sha(saved)
    assert meta["defaults"]["settings"]["notify"]["min_score"] == 70
    assert "apply" not in meta["defaults"]["settings"]
    assert meta["sources"]["linkedin"] == "LinkedIn"
    with Database(env.db_path).session() as s:
        assert s.scalars(select(SearchQuery.keywords)).all() == ["backend engineer"]

    log = capsys.readouterr().out
    assert "Using the settings saved from your board." in log
    for private in ("Backend", "Acme", "Globex", "backend engineer", "from the secret"):
        assert private not in log


def test_the_settings_version_is_the_one_github_uses(tmp_path):
    # GitHub names file versions by their git blob ID; the page sends it back when saving, so
    # settings changed elsewhere in the meantime aren't overwritten without asking.
    saved = saved_settings(tmp_path / CONFIG_FILE, CONFIG)
    git = subprocess.run(["git", "hash-object", str(saved)], capture_output=True, text=True, check=True)
    assert git_blob_sha(saved) == git.stdout.strip()


async def test_a_new_board_asks_for_settings(tmp_path, env, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/job-search")
    site = tmp_path / "site"
    missing = tmp_path / "repo" / CONFIG_FILE
    assert await publish(env, site, tmp_path / "state.json", None, PASSPHRASE, config_file=missing) == 0

    payload = open_envelope(read_envelope(site / "index.html"), PASSPHRASE)
    meta = payload["settings"]
    assert meta["needs_setup"] and meta["origin"] is None and meta["config"] == {} and meta["sha"] is None
    assert payload["scan"]["status"] is None and payload["jobs"] == []  # nothing to scan, so no scan
    assert "Nothing to scan yet" in capsys.readouterr().out


@respx.mock
async def test_settings_that_cant_be_decrypted_keep_the_last_ones(tmp_path, env, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/job-search")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    mock_sources()
    site, state = tmp_path / "site", tmp_path / "state.json"
    assert await publish(env, site, state, encode_config(CONFIG), PASSPHRASE) == 0

    # Saved with another passphrase, and the secret is gone.
    saved = saved_settings(tmp_path / "repo" / CONFIG_FILE, CONFIG, passphrase="an older passphrase")
    assert await publish(env, site, state, None, PASSPHRASE, config_file=saved) == 0

    log = capsys.readouterr().out
    assert "Using the settings from the last run." in log and "1 problem(s) with your settings" in log
    payload = open_envelope(read_envelope(site / "index.html"), PASSPHRASE)
    meta = payload["settings"]
    assert meta["unreadable"] and meta["origin"] == "state" and not meta["needs_setup"]
    assert {c["slug"] for c in meta["config"]["companies"]} == {"acme", "globex"}  # the last run's
    assert payload["scan"]["status"] == "ok"
    assert any("Couldn't decrypt the settings" in e["error"] for e in payload["scan"]["errors"])


async def test_invalid_settings_are_explained_on_the_board(tmp_path, env, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/job-search")
    site = tmp_path / "site"
    bad = json.dumps({"profile": {"years_experience": "plenty"}, "companies": ["https://jobs.lever.co/x"]})
    assert await publish(env, site, tmp_path / "state.json", bad, PASSPHRASE) == 0

    payload = open_envelope(read_envelope(site / "index.html"), PASSPHRASE)
    errors = [e["error"] for e in payload["scan"]["errors"]]
    assert any(e.startswith("Your profile isn't valid (profile.years_experience:") for e in errors)
    assert payload["settings"]["config"]["profile"]["years_experience"] == "plenty"  # to fix in Settings
    assert payload["settings"]["needs_setup"]
    log = capsys.readouterr().out
    assert "1 problem(s) with your settings" in log and "plenty" not in log


CORS = {"Access-Control-Allow-Origin": "*"}


def with_settings(config: dict, **meta) -> dict:
    payload = sample_payload()
    payload["generated"] = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    options = {"unreadable": False, "needs_setup": False, "repository": "someone/job-search", "sha": None}
    payload["settings"] = board_settings(config, meta.pop("origin", "board"), **{**options, **meta})
    return payload


class FakeGitHub:
    """Stands in for the GitHub contents API the Settings page saves through."""

    def __init__(self, file: str = "", sha: str = "sha-remote"):
        self.file, self.sha = file, sha
        self.puts: list[tuple[dict, dict]] = []
        self.replies: list[tuple[int, dict]] = []  # canned answers for the next PUTs

    def __call__(self, route) -> None:
        request = route.request
        if request.method == "GET":
            content = base64.encodebytes(self.file.encode()).decode()  # GitHub wraps it in lines
            route.fulfill(json={"sha": self.sha, "content": content, "encoding": "base64"}, headers=CORS)
        elif request.method == "PUT":
            self.puts.append((json.loads(request.post_data), request.headers))
            status, reply = (
                self.replies.pop(0) if self.replies else (201, {"content": {"sha": f"sha-{len(self.puts)}"}})
            )
            route.fulfill(status=status, json=reply, headers=CORS)
        else:
            route.fulfill(status=405, headers=CORS)

    def saved(self, index: int = -1) -> dict:
        body = self.puts[index][0]
        return open_envelope(json.loads(base64.b64decode(body["content"])), PASSPHRASE)


@requires_browser
def test_settings_are_edited_and_saved_from_the_board(serve_board):
    from playwright.sync_api import expect, sync_playwright

    config = {**CONFIG, "custom": {"kept": True}}  # fields the form doesn't show are kept as they are
    url = serve_board(with_settings(config, origin="secret"))
    github = FakeGitHub()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.route("https://api.github.com/**", github)
        page.goto(url)
        page.fill("#passphrase", PASSPHRASE)
        page.check("#remember")
        page.click("#unlock-button")
        expect(page.locator("#jobs-view")).to_be_visible()

        page.click("#settings-button")
        expect(page.locator("#settings-view")).to_contain_text("JOBBOT_CONFIG secret")
        field = lambda path: page.locator(f'[data-path="{path}"]')  # noqa: E731
        expect(field("profile.target_titles")).to_have_value(
            "Backend Engineer\nData Engineer\nSoftware Engineer"
        )
        expect(field("profile.years_experience")).to_have_value("3")
        expect(field("settings.notify.min_score")).to_have_value("70")  # a default: not in the config
        expect(field("settings.filters.max_age_days")).to_have_value("")  # turned off in the config
        expect(page.locator("#companies .row")).to_have_count(2)
        expect(page.locator("#searches .row")).to_have_count(1)

        field("profile.target_titles").fill("Backend Engineer\n Platform Engineer \n\n")
        field("settings.filters.locations").fill("Austin, TX\nRemote")
        field("settings.filters.min_salary").fill("$120k")
        page.locator("#companies .row").first.get_by_role("button", name="Remove").click()
        page.get_by_role("button", name="Add a company").click()
        added = page.locator("#companies .row").last
        added.get_by_label("Company name").fill("OpenAI")
        added.get_by_label("Careers page or job board URL").fill("jobs.ashbyhq.com/openai")
        search = page.locator("#searches .row").first
        search.get_by_label("LinkedIn").check()
        page.get_by_role("button", name="Add a search").click()
        page.locator("#searches .row").last.get_by_label("Keywords").fill("platform engineer")

        # Problems are caught before anything is sent.
        field("settings.notify.webhook_url").fill("discord")
        page.click("#save-settings")
        expect(page.locator("#settings-message")).to_contain_text("Webhook URL: enter a full web address")
        field("settings.notify.webhook_url").fill("")
        page.click("#save-settings")
        expect(page.locator("#settings-message")).to_contain_text("paste a GitHub token")
        assert github.puts == []

        page.fill("#github-token", "github_pat_TEST_TOKEN_123")
        page.click("#save-settings")
        expect(page.locator("#settings-message")).to_contain_text("Saved.")
        expect(page.locator("#pending")).to_contain_text("You saved new settings")
        body, headers = github.puts[0]
        assert headers["authorization"] == "Bearer github_pat_TEST_TOKEN_123"
        assert body["message"] == "Update job board settings" and "sha" not in body  # a new file
        doc = github.saved()
        assert doc["profile"]["target_titles"] == ["Backend Engineer", "Platform Engineer"]
        assert doc["profile"]["email"] == "jane@example.com"
        assert doc["settings"]["filters"]["locations"] == ["Austin, TX", "Remote"]
        assert doc["settings"]["filters"]["min_salary"] == 120000
        assert doc["settings"]["filters"]["max_age_days"] is None
        assert doc["settings"]["notify"] == {"webhook_url": "", "min_score": 70}
        assert doc["companies"] == [
            {"name": "Globex Corp", "url": "https://jobs.lever.co/globex"},
            {"name": "OpenAI", "url": "https://jobs.ashbyhq.com/openai"},
        ]
        assert doc["searches"] == [
            {
                "keywords": "backend engineer",
                "location": "New York",
                "remote_only": False,
                "sources": ["linkedin", "remotive"],
            },
            {
                "keywords": "platform engineer",
                "location": "",
                "remote_only": False,
                "sources": ["linkedin", "indeed"],
            },
        ]
        assert doc["custom"] == {"kept": True} and doc["resume_text"] == CONFIG["resume_text"]

        # The token and the pending settings are remembered, encrypted.
        stored = page.evaluate("JSON.stringify(localStorage)")
        assert "jobbot:token:" in stored and "jobbot:pending:" in stored
        for secret in ("github_pat_TEST_TOKEN_123", "Platform Engineer", "OpenAI"):
            assert secret not in stored

        # Saving again names the version it saved, so changes made elsewhere aren't overwritten
        # without asking.
        github.replies.append((409, {"message": "is at 1234 but expected sha-1"}))
        page.click("#save-settings")
        expect(page.locator("#settings-message")).to_contain_text("changed somewhere else")
        assert github.puts[1][0]["sha"] == "sha-1"
        page.click("#save-settings")
        expect(page.locator("#settings-message")).to_contain_text("Saved.")
        assert github.puts[2][0]["sha"] == "sha-remote"
        assert github.puts[2][1]["authorization"] == "Bearer github_pat_TEST_TOKEN_123"

        # Until the new scan is published, a reload shows the saved settings, not the old ones.
        page.reload()
        expect(page.locator("#pending")).to_be_visible()
        page.click("#settings-button")
        expect(field("profile.target_titles")).to_have_value("Backend Engineer\nPlatform Engineer")
        expect(page.locator("#token-status")).to_contain_text("saved on this device")

        # An expired token is forgotten.
        github.replies.append((401, {"message": "Bad credentials"}))
        page.click("#save-settings")
        expect(page.locator("#settings-message")).to_contain_text("didn't accept the token")
        assert github.puts[3][0]["sha"] == "sha-3"
        expect(page.locator("#token-status")).to_have_count(0)
        assert "jobbot:token:" not in page.evaluate("JSON.stringify(localStorage)")

        # Settings can be imported, like the compact text the jobbot app copies.
        example = json.loads(
            (Path(__file__).parents[1] / "examples" / "jobbot-config.example.json").read_text()
        )
        page.fill("#settings-json", encode_config(example))
        page.get_by_role("button", name="Load into the form").click()
        expect(page.locator("#settings-message")).to_contain_text("Loaded.")
        expect(field("profile.target_titles")).to_have_value("\n".join(example["profile"]["target_titles"]))
        expect(page.locator("#companies .row")).to_have_count(3)
        page.get_by_role("button", name="Show these settings as JSON").click()
        assert "_help" not in json.loads(page.locator("#settings-json").input_value())
        assert errors == []
        browser.close()


@requires_browser
def test_a_new_board_opens_settings_and_recovers_old_ones(serve_board):
    from playwright.sync_api import expect, sync_playwright

    payload = with_settings({}, origin=None, needs_setup=True, unreadable=True, sha="sha-old")
    payload["jobs"], payload["hidden"] = [], []
    url = serve_board(payload)
    salt = site_salt("someone/job-search")
    github = FakeGitHub(encrypt_config(CONFIG, derive_key("the old passphrase", salt), salt), sha="sha-old")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.route("https://api.github.com/**", github)
        page.goto(url)
        page.fill("#passphrase", PASSPHRASE)
        page.click("#unlock-button")

        # A board with nothing to scan opens Settings, with the defaults filled in.
        expect(page.locator("#welcome")).to_be_visible()
        titles = page.locator('[data-path="profile.target_titles"]')
        expect(titles).to_have_value("")
        expect(page.locator('[data-path="settings.filters.max_years_gap"]')).to_have_value("2")
        expect(page.locator('[data-path="settings.filters.include_remote"]')).to_be_checked()

        recovery = page.locator("#recovery")
        recovery.get_by_label("Old passphrase").fill("not it either")
        recovery.get_by_role("button", name="Recover").click()
        expect(recovery).to_contain_text("didn't work either")
        recovery.get_by_label("Old passphrase").fill("the old passphrase")
        recovery.get_by_role("button", name="Recover").click()
        expect(titles).to_have_value("Backend Engineer\nData Engineer\nSoftware Engineer")
        expect(page.locator("#recovery")).to_have_count(0)
        expect(page.locator("#settings-message")).to_contain_text("Recovered")

        page.fill("#github-token", "github_pat_TEST")
        page.uncheck("#remember-token")
        page.click("#save-settings")
        expect(page.locator("#settings-message")).to_contain_text("Saved.")
        assert github.puts[0][0]["sha"] == "sha-old"  # replaces the file it recovered
        assert github.saved()["profile"]["target_titles"] == CONFIG["profile"]["target_titles"]
        assert "jobbot:token:" not in page.evaluate("JSON.stringify(localStorage)")  # not remembered
        expect(page.locator("#token-status")).to_contain_text("until you leave this page")

        page.get_by_role("button", name="Back to jobs").click()
        expect(page.locator("#list")).to_contain_text("No jobs yet")
        assert errors == []
        browser.close()

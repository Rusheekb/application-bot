"""The hosted job board: the shared feed the scan publishes, and the site it writes."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
import respx
from sqlalchemy import select

from jobbot.db import Database, get_settings
from jobbot.models import Company, Job, SearchQuery, utcnow
from jobbot.publish import (
    BOARD_FILES,
    DEFAULT_KEEP_TITLES,
    MAX_DESCRIPTION,
    PublishError,
    decode_config,
    encode_config,
    export_config,
    feed_config,
    feed_payload,
    import_config,
    load_sources,
    publish,
    restore_state,
    save_state,
)
from tests.conftest import load_json

ROOT = Path(__file__).parents[1]
# A setup exported from the local app.
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
SOURCES = {
    "keep_titles": ["engineer"],
    "max_age_days": None,  # the test postings are old
    "companies": CONFIG["companies"],
    "searches": CONFIG["searches"],
}


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


async def test_invalid_settings_are_explained(db):
    with pytest.raises(PublishError, match=r"profile isn't valid \(profile.years_experience:"):
        await import_config(db, {"profile": {"years_experience": "plenty"}})
    with pytest.raises(PublishError, match="must be lists"):
        await import_config(db, {"companies": "https://jobs.lever.co/x"})


def test_load_sources_checks_the_file(tmp_path):
    path = tmp_path / "sources.json"
    with pytest.raises(PublishError, match="missing"):
        load_sources(path)
    for text, problem in (
        ("{nope", "isn't valid JSON"),
        ("[]", "JSON object"),
        ('{"companies": "https://jobs.lever.co/x"}', "'companies' must be a list"),
        ('{"keep_titles": [1]}', "list of strings"),
        ('{"max_age_days": 0}', "whole number"),
    ):
        path.write_text(text)
        with pytest.raises(PublishError, match=problem):
            load_sources(path)
    path.write_text('{"companies": ["https://jobs.lever.co/x"]}')
    sources = load_sources(path)
    assert sources["keep_titles"] == DEFAULT_KEEP_TITLES and sources["max_age_days"] == 45
    config = feed_config(sources)
    # Nobody's profile, and no filters but age: visitors filter in their own browsers.
    assert config["profile"] == {} and config["settings"]["ai"] == {"enabled": False}
    assert config["settings"]["filters"]["max_years_gap"] is None


async def test_the_shipped_sources_are_valid(db):
    sources = load_sources(ROOT / "feed" / "sources.json")
    assert await import_config(db, feed_config(sources)) == []
    with db.session() as s:
        assert len(s.scalars(select(Company)).all()) == len(sources["companies"])
        assert len(s.scalars(select(SearchQuery)).all()) == len(sources["searches"])


async def test_example_config_is_valid(db):
    config = decode_config((ROOT / "examples" / "jobbot-config.example.json").read_text())
    assert await import_config(db, config) == []
    with db.session() as s:
        assert {c.ats for c in s.scalars(select(Company))} == {"greenhouse", "lever", "ashby"}
        assert len(s.scalars(select(SearchQuery)).all()) == 2


@respx.mock
async def test_publish_builds_the_public_board(tmp_path, env, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/job-search")
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").respond(
        json=load_json("greenhouse_jobs.json")
    )
    respx.get("https://api.lever.co/v0/postings/globex").respond(json=load_json("lever_postings.json"))
    respx.get(url__startswith="https://remotive.com/api/remote-jobs").respond(json={"jobs": []})
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps(SOURCES))
    site, state = tmp_path / "site", tmp_path / "state" / "state.db.gz"

    assert await publish(env, site, state, sources) == 0

    assert {p.name for p in site.iterdir()} == {*BOARD_FILES, "jobs.json", ".nojekyll"}
    index = (site / "index.html").read_text()
    assert "__VERSION__" not in index and "__VERSION__" not in (site / "app.js").read_text()
    assert re.search(r'src="app\.js\?v=[0-9a-f]{10}"', index)

    payload = json.loads((site / "jobs.json").read_text())
    jobs = {j["title"]: j for j in payload["jobs"]}
    # Only titles matching keep_titles (or a search's keywords) are kept from company boards.
    assert "Account Executive" not in jobs
    assert {"Backend Engineer", "Data Engineer", "Frontend Engineer (Contract)"} <= set(jobs)
    # Nobody's filters apply in the feed: the 10+ year job is there, for the browser to judge.
    senior = jobs["Senior Staff Software Engineer, Infrastructure"]
    assert senior["years"] == 10 and senior["seniority"] == "staff"
    data = jobs["Data Engineer"]
    assert (data["workplace"], data["company"], data["sources"]) == ("hybrid", "Globex Corp", ["Lever"])
    assert data["url"].startswith("https://") and data["found"].endswith("Z")
    assert all(len(j["description"]) <= MAX_DESCRIPTION for j in payload["jobs"])
    assert all(isinstance(j["no_sponsorship"], bool) for j in payload["jobs"])
    assert all(isinstance(j["clearance"], bool) for j in payload["jobs"])
    assert payload["repository"] == "someone/job-search"
    assert payload["sources"] == {"companies": 2, "searches": ["backend engineer · New York"]}
    assert payload["scan"]["status"] == "ok"
    assert any("myspace" in e["error"] for e in payload["scan"]["errors"])

    log = capsys.readouterr().out
    assert "No saved job history yet" in log and "Published the job board." in log

    # The next run picks up where this one left off.
    assert await publish(env, site, state, sources) == 0
    assert "Restored the job history" in capsys.readouterr().out
    payload = json.loads((site / "jobs.json").read_text())
    assert payload["scan"]["stats"]["new"] == 0
    assert {"Backend Engineer", "Data Engineer"} <= {j["title"] for j in payload["jobs"]}


async def test_publish_reports_a_broken_sources_file(tmp_path, env):
    sources = tmp_path / "sources.json"
    sources.write_text('{"companies": [], "searches": []}')
    with pytest.raises(PublishError, match="no companies or searches"):
        await publish(env, tmp_path / "site", tmp_path / "state.db.gz", sources)


def test_state_round_trip(tmp_path, db):
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
    state = tmp_path / "state" / "state.db.gz"
    db.engine.dispose()
    save_state(db.path, state)
    restored = tmp_path / "restored" / "jobbot.db"
    assert restore_state(state, restored) == "restored"
    with Database(restored).session() as s:
        assert s.scalars(select(Job.title)).all() == ["Backend Engineer"]
    # Earlier versions saved an encrypted file here: start fresh instead of failing.
    old = tmp_path / "old.json"
    old.write_text('{"v": 1, "iv": "abc", "data": "def"}')
    assert restore_state(old, tmp_path / "other.db") == "unreadable"
    assert restore_state(tmp_path / "missing.gz", tmp_path / "other.db") == "missing"


def test_the_feed_skips_closed_and_stale_jobs(db):
    now = utcnow()
    with db.session() as s:
        base = {"source": "lever", "company": "Globex"}
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
    jobs = {j["title"]: j for j in feed_payload(db)["jobs"]}
    assert set(jobs) == {"Current", "Bad link"}
    assert jobs["Bad link"]["url"] is None


def test_the_page_only_talks_to_its_own_site_and_anthropic():
    board = ROOT / "src" / "jobbot" / "board"
    index = (board / "index.html").read_text()
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', index).group(1)
    assert "script-src 'self';" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "connect-src 'self' https://api.anthropic.com;" in csp
    assert not re.search(r"<script(?![^>]*\bsrc=)", index)  # no inline scripts
    for name in ("app.js", "claude.js", "match.js"):
        source = (board / name).read_text()
        assert "innerHTML" not in source and "eval(" not in source
        fetches = re.findall(r"fetch\(([^,)]+)", source)
        assert all(f in ('"jobs.json"', "`${API}${path}`") for f in fetches), fetches
    assert 'API = "https://api.anthropic.com/v1"' in (board / "claude.js").read_text()

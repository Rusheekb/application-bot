from __future__ import annotations

import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from jobbot.config import Env
from jobbot.db import Database

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str):
    return json.loads((FIXTURES / "api" / name).read_text())


@pytest.fixture
def env(tmp_path: Path) -> Env:
    e = Env(data_dir=tmp_path / "data")
    e.ensure_dirs()
    return e


@pytest.fixture
def db(env: Env) -> Database:
    database = Database(env.db_path)
    database.create_all()
    return database


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep test output clean
        pass


@pytest.fixture(scope="session")
def form_server() -> str:
    """Serve tests/fixtures/forms over HTTP; yields the base URL."""
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES / "forms"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
    except Exception:
        return False
    return True


requires_browser = pytest.mark.skipif(not playwright_available(), reason="Playwright Chromium not available")

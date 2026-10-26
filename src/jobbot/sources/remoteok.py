"""Remote OK public API (https://remoteok.com/api). Free; Remote OK asks that you link back to them."""

from __future__ import annotations

import httpx

from jobbot.config import Env
from jobbot.sources.base import BROWSER_USER_AGENT, JobPosting, SearchRef, request_json
from jobbot.text import html_to_text, parse_datetime, title_tokens

KEY = "remoteok"
LABEL = "Remote OK"
API = "https://remoteok.com/api"


def configured(env: Env) -> bool:
    return True


async def search(client: httpx.AsyncClient, query: SearchRef, env: Env) -> list[JobPosting]:
    data = await request_json(client, API, headers={"User-Agent": BROWSER_USER_AGENT})
    if not isinstance(data, list):
        return []
    wanted = [w for w in title_tokens(query.keywords) if w not in {"remote"}]
    results = []
    for item in data:
        posting = parse_job(item)
        if posting is None:
            continue
        haystack = set(title_tokens(posting.title + " " + " ".join(item.get("tags") or [])))
        if all(w in haystack for w in wanted):
            results.append(posting)
        if len(results) >= query.results:
            break
    return results


def parse_job(item: dict) -> JobPosting | None:
    if not isinstance(item, dict) or "legal" in item:
        return None
    job_id = str(item.get("id") or "")
    title = (item.get("position") or "").strip()
    url = item.get("url") or item.get("apply_url")
    if not job_id or not title or not url:
        return None
    return JobPosting(
        source=KEY,
        source_id=job_id,
        title=title,
        company=(item.get("company") or "Unknown").strip(),
        url=url,
        location=f"Remote ({item['location']})" if item.get("location") else "Remote",
        workplace="remote",
        description=html_to_text(item.get("description")),
        posted_at=parse_datetime(item.get("date") or item.get("epoch")),
        salary_min=item.get("salary_min") or None,
        salary_max=item.get("salary_max") or None,
        salary_currency="USD" if item.get("salary_min") or item.get("salary_max") else None,
        salary_period="year" if item.get("salary_min") or item.get("salary_max") else None,
    )

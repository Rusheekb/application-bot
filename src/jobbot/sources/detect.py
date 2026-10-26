"""Work out which applicant tracking system (ATS) hosts a careers page, from its URL or HTML."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import httpx

from jobbot.sources.base import BROWSER_USER_AGENT, CompanyRef, SourceError

_LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")
_WORKDAY_HOST = re.compile(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$")
_URL_IN_HTML = re.compile(r"https?://[^\s\"'<>\\)]+")


def parse_ats_url(url: str) -> CompanyRef | None:
    """Recognize a careers-site or job URL on a supported ATS and return the company it belongs to."""
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    query = parse_qs(parsed.query)

    if host.endswith("greenhouse.io"):
        region = {"region": "eu"} if ".eu." in host else {}
        if host.startswith("boards-api.") and len(parts) >= 3 and parts[:2] == ["v1", "boards"]:
            return CompanyRef("greenhouse", parts[2], config=region)
        if parts and parts[0] == "embed":
            token = (query.get("for") or [None])[0]
            return CompanyRef("greenhouse", token, config=region) if token else None
        if parts and host.split(".")[0] in ("boards", "job-boards"):
            return CompanyRef("greenhouse", parts[0], config=region)
        return None

    if host in ("jobs.lever.co", "jobs.eu.lever.co") and parts:
        return CompanyRef("lever", parts[0], config={"region": "eu"} if ".eu." in host else {})
    if host in ("api.lever.co", "api.eu.lever.co") and len(parts) >= 3 and parts[:2] == ["v0", "postings"]:
        return CompanyRef("lever", parts[2], config={"region": "eu"} if ".eu." in host else {})

    if host == "jobs.ashbyhq.com" and parts:
        return CompanyRef("ashby", parts[0])
    if host == "api.ashbyhq.com" and "job-board" in parts:
        idx = parts.index("job-board")
        return CompanyRef("ashby", parts[idx + 1]) if idx + 1 < len(parts) else None

    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and parts:
        if parts[0] == "oneclick-ui" and "company" in parts:
            idx = parts.index("company")
            return CompanyRef("smartrecruiters", parts[idx + 1]) if idx + 1 < len(parts) else None
        return CompanyRef("smartrecruiters", parts[0])
    if host == "api.smartrecruiters.com" and len(parts) >= 3 and parts[:2] == ["v1", "companies"]:
        return CompanyRef("smartrecruiters", parts[2])

    if host == "apply.workable.com" and parts:
        if parts[0] == "api":
            if "accounts" in parts:
                idx = parts.index("accounts")
                return CompanyRef("workable", parts[idx + 1]) if idx + 1 < len(parts) else None
            return None
        if parts[0] == "j":
            return None  # bare job shortlinks don't name the account
        return CompanyRef("workable", parts[0])
    if host.endswith(".workable.com") and host.split(".")[0] not in ("apply", "www", "jobs", "help"):
        return CompanyRef("workable", host.split(".")[0])

    if m := _WORKDAY_HOST.match(host):
        tenant = m.group(1)
        if parts and parts[0] == "wday" and len(parts) >= 4 and parts[1] == "cxs":
            tenant, site = parts[2], parts[3]
        else:
            rest = [p for p in parts if not _LOCALE.match(p)]
            if not rest:
                return None
            site = rest[0]
        return CompanyRef(
            "workday", f"{tenant}/{site}", name=tenant, config={"host": host, "tenant": tenant, "site": site}
        )
    if host.endswith(".myworkdaysite.com"):
        rest = [p for p in parts if not _LOCALE.match(p)]
        if len(rest) >= 3 and rest[0] == "recruiting":
            tenant, site = rest[1], rest[2]
            return CompanyRef(
                "workday",
                f"{tenant}/{site}",
                name=tenant,
                config={"host": host, "tenant": tenant, "site": site},
            )
        return None

    if host.endswith(".recruitee.com") and host.split(".")[0] not in ("www", "api", "app"):
        return CompanyRef("recruitee", host.split(".")[0])

    return None


def find_ats_in_html(html: str) -> CompanyRef | None:
    """Find an embedded or linked ATS board in a company's own careers page."""
    counts: dict[tuple[str, str], tuple[int, CompanyRef]] = {}
    for raw in _URL_IN_HTML.findall(html):
        ref = parse_ats_url(raw.replace("&amp;", "&"))
        if ref is None:
            continue
        key = (ref.ats, ref.slug.lower())
        n, _ = counts.get(key, (0, ref))
        counts[key] = (n + 1, ref)
    # Greenhouse embeds are often configured in JS rather than as a URL.
    if not counts and (m := re.search(r"boardToken['\"]?\s*[:=]\s*['\"]([A-Za-z0-9_-]+)", html)):
        return CompanyRef("greenhouse", m.group(1))
    if not counts:
        return None
    return max(counts.values(), key=lambda item: item[0])[1]


async def detect_company(client: httpx.AsyncClient, url: str) -> CompanyRef:
    """Resolve a careers URL to a supported ATS board, fetching the page if needed."""
    url = url.strip()
    if ref := parse_ats_url(url):
        return ref
    target = url if "://" in url else "https://" + url
    parsed = urlparse(target)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or "." not in parsed.hostname
        or " " in url
    ):
        raise SourceError("that doesn't look like a web address; paste the full URL of the careers page")
    try:
        response = await client.get(target, headers={"User-Agent": BROWSER_USER_AGENT}, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise SourceError(f"couldn't load {url}: {exc.__class__.__name__}") from exc
    if ref := parse_ats_url(str(response.url)):
        return ref
    if response.status_code >= 400:
        raise SourceError(f"couldn't load {url} (HTTP {response.status_code})")
    if ref := find_ats_in_html(response.text):
        return ref
    raise SourceError(
        "couldn't find a supported job board (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, "
        "Workable, Recruitee) on that page; try the URL of the company's job board itself"
    )


_APPLY_HOSTS = [
    ("greenhouse.io", "greenhouse"),
    ("lever.co", "lever"),
    ("ashbyhq.com", "ashby"),
    ("workable.com", "workable"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("myworkdayjobs.com", "workday"),
    ("myworkdaysite.com", "workday"),
    ("recruitee.com", "recruitee"),
    ("icims.com", "icims"),
    ("taleo.net", "taleo"),
    ("successfactors.com", "successfactors"),
    ("successfactors.eu", "successfactors"),
    ("jobvite.com", "jobvite"),
    ("bamboohr.com", "bamboohr"),
    ("breezy.hr", "breezy"),
    ("jazzhr.com", "jazzhr"),
    ("applytojob.com", "jazzhr"),
    ("linkedin.com", "linkedin"),
    ("indeed.com", "indeed"),
]


def ats_for_url(url: str | None) -> str | None:
    """Which ATS (or job board) an application URL belongs to, if recognizable."""
    if not url:
        return None
    parsed = urlparse(url if "://" in url else "https://" + url)
    host = (parsed.hostname or "").lower()
    for suffix, ats in _APPLY_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return ats
    if "gh_jid=" in parsed.query:
        return "greenhouse"
    return None

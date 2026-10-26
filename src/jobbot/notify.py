"""Webhook notifications for new high-scoring jobs (Discord, Slack, ntfy or any JSON endpoint)."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from jobbot.models import Job
from jobbot.text import format_salary

log = logging.getLogger(__name__)


def _job_line(job: Job, base_url: str) -> str:
    """One line per job, linking to the job in the web app, or to the posting without a base URL."""
    bits = [f"{job.score}" if job.score is not None else "–", f"{job.title} @ {job.company}"]
    if job.location:
        bits.append(job.location)
    salary = format_salary(job.salary_min, job.salary_max, job.salary_currency, job.salary_period)
    if salary:
        bits.append(salary)
    link = f"{base_url.rstrip('/')}/jobs/{job.id}" if base_url else (job.apply_url or job.url)
    return f"• {' · '.join(bits)}\n  {link}"


def build_message(jobs: Sequence[Job], base_url: str, board_url: str = "") -> tuple[str, str]:
    title = f"{len(jobs)} new job match{'es' if len(jobs) != 1 else ''}"
    lines = [_job_line(j, base_url) for j in jobs[:10]]
    if len(jobs) > 10:
        lines.append(f"…and {len(jobs) - 10} more")
    if board_url:
        lines.append(f"Your job board: {board_url}")
    return title, "\n".join(lines)


async def send_webhook(
    client: httpx.AsyncClient, url: str, jobs: Sequence[Job], base_url: str = "", board_url: str = ""
) -> None:
    if not url or not jobs:
        return
    title, body = build_message(jobs, base_url, board_url)
    # Job titles come from the internet: don't let one ping @everyone or a Slack channel.
    if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
        response = await client.post(
            url, json={"content": f"**{title}**\n{body}"[:1990], "allowed_mentions": {"parse": []}}
        )
    elif "hooks.slack.com" in url:
        text = f"*{title}*\n{body}".replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        response = await client.post(url, json={"text": text})
    elif "ntfy" in url:
        response = await client.post(
            url, content=body.encode(), headers={"Title": title, "Tags": "briefcase"}
        )
    else:
        payload = {
            "title": title,
            "text": body,
            "jobs": [
                {"id": j.id, "title": j.title, "company": j.company, "score": j.score, "url": j.url}
                for j in jobs
            ],
        }
        response = await client.post(url, json=payload)
    if response.status_code >= 400:
        log.warning("Notification webhook returned HTTP %s", response.status_code)

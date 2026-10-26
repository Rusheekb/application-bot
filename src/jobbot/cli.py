"""Command-line interface: `jobbot serve`, `jobbot scan`, `jobbot apply`, ..."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import textwrap

from sqlalchemy import or_, select

from jobbot import __version__
from jobbot.config import Env, get_env


def _db(env: Env):
    from jobbot.db import Database

    env.ensure_dirs()
    db = Database(env.db_path)
    db.create_all()
    return db


def cmd_serve(args: argparse.Namespace, env: Env) -> int:
    import uvicorn

    from jobbot.web.app import create_app

    host = args.host or env.host
    port = args.port or env.port
    env = env.model_copy(update={"host": host, "port": port})
    if host not in ("127.0.0.1", "localhost", "::1") and not env.password:
        logging.warning(
            "jobbot is listening on %s without a password. Anyone who can reach this address can see your "
            "profile and act on your behalf. Set JOBBOT_PASSWORD in .env.",
            host,
        )
    app = create_app(env, scheduler=not args.no_scheduler)
    print(
        f"jobbot {__version__} running at http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}"
    )
    uvicorn.run(app, host=host, port=port, log_level=env.log_level.lower(), access_log=False)
    return 0


def cmd_scan(args: argparse.Namespace, env: Env) -> int:
    from jobbot.models import Job, ScanRun
    from jobbot.pipeline import Scanner

    db = _db(env)
    print("Scanning…")
    scan_id = asyncio.run(Scanner(db, env).run("cli"))
    with db.session() as s:
        scan = s.get(ScanRun, scan_id)
        stats = scan.stats or {}
        print(
            f"Done ({scan.status}): {stats.get('fetched', 0)} jobs fetched, {stats.get('new', 0)} new, "
            f"{stats.get('hidden', 0)} hidden by filters, {stats.get('scored', 0)} analyzed by Claude, "
            f"{stats.get('closed', 0)} closed."
        )
        for error in scan.errors or []:
            print(f"  ! {error['source']}: {error['error']}")
        new_jobs = list(
            s.scalars(
                select(Job)
                .where(
                    Job.first_seen_at >= scan.started_at,
                    or_(Job.hidden_reason.is_(None), Job.show_anyway.is_(True)),
                )
                .order_by(Job.score.desc().nulls_last())
                .limit(10)
            )
        )
    if new_jobs:
        print("\nTop new jobs:")
        for job in new_jobs:
            print(
                f"  [{job.score or '-':>3}] {job.title} @ {job.company} ({job.location or 'n/a'})  #{job.id}"
            )
    return 0 if scan.status != "error" else 1


def cmd_jobs(args: argparse.Namespace, env: Env) -> int:
    from jobbot.models import Job

    db = _db(env)
    with db.session() as s:
        stmt = select(Job).where(or_(Job.hidden_reason.is_(None), Job.show_anyway.is_(True)))
        if args.status != "all":
            stmt = stmt.where(Job.status == args.status)
        if args.min_score:
            stmt = stmt.where(Job.score >= args.min_score)
        jobs = list(s.scalars(stmt.order_by(Job.score.desc().nulls_last()).limit(args.limit)))
    if not jobs:
        print("No jobs.")
    for job in jobs:
        print(f"#{job.id:<5} [{job.score or '-':>3}] {job.title} @ {job.company} · {job.location or 'n/a'}")
        print(f"        {job.apply_url or job.url}")
    return 0


async def _detect(url: str):
    from jobbot.sources.base import new_http_client
    from jobbot.sources.detect import detect_company

    async with new_http_client() as client:
        return await detect_company(client, url)


def cmd_add_company(args: argparse.Namespace, env: Env) -> int:
    from jobbot.models import Company
    from jobbot.sources import SourceError, source_label

    db = _db(env)
    try:
        ref = asyncio.run(_detect(args.url))
    except SourceError as exc:
        print(f"Couldn't add it: {exc}")
        return 1
    with db.session() as s:
        if s.scalars(select(Company).where(Company.ats == ref.ats, Company.slug == ref.slug)).first():
            print("Already on your list.")
            return 0
        name = args.name or ref.name or ref.slug.split("/")[0].replace("-", " ").title()
        s.add(Company(name=name, ats=ref.ats, slug=ref.slug, config=ref.config, careers_url=args.url))
    print(f"Added {name} ({source_label(ref.ats)}: {ref.slug}).")
    return 0


def cmd_check_company(args: argparse.Namespace, env: Env) -> int:
    """Fetch a board without saving anything: a quick way to confirm a connector works."""
    from jobbot.db import get_profile, get_settings
    from jobbot.matching import is_relevant, relevance_terms
    from jobbot.sources import COMPANY_SOURCES, SourceError, source_label
    from jobbot.sources.base import new_http_client

    db = _db(env)
    with db.session() as s:
        profile, settings = get_profile(s), get_settings(s)
    terms = relevance_terms(settings.filters, profile, [])

    async def run():
        ref = await _detect(args.url)
        ref.search_terms = terms if ref.ats == "workday" else []
        async with new_http_client() as client:
            return ref, await COMPANY_SOURCES[ref.ats].fetch_jobs(client, ref)

    try:
        ref, postings = asyncio.run(run())
    except SourceError as exc:
        print(f"Error: {exc}")
        return 1
    matching = [p for p in postings if is_relevant(p.title, terms)]
    print(
        f"{source_label(ref.ats)} board '{ref.slug}': {len(postings)} open jobs, {len(matching)} matching your target titles."
    )
    for p in (matching or postings)[:15]:
        described = "with description" if p.description else "description fetched later"
        print(f"  - {p.title} · {p.location or 'n/a'} ({described})")
    return 0


def cmd_add_search(args: argparse.Namespace, env: Env) -> int:
    from jobbot.models import SearchQuery
    from jobbot.sources import DEFAULT_SEARCH_SOURCES, SEARCH_SOURCE_LABELS

    sources = [s.strip() for s in args.sources.split(",")] if args.sources else list(DEFAULT_SEARCH_SOURCES)
    unknown = [s for s in sources if s not in SEARCH_SOURCE_LABELS]
    if unknown:
        print(f"Unknown sources: {', '.join(unknown)}. Choose from: {', '.join(SEARCH_SOURCE_LABELS)}")
        return 1
    db = _db(env)
    with db.session() as s:
        s.add(
            SearchQuery(
                keywords=args.keywords, location=args.location, remote_only=args.remote, sources=sources
            )
        )
    print(f"Added search '{args.keywords}' on {', '.join(SEARCH_SOURCE_LABELS[s] for s in sources)}.")
    return 0


def cmd_apply(args: argparse.Namespace, env: Env) -> int:
    from jobbot.apply.manager import ApplyManager
    from jobbot.models import ApplicationRun, Job

    db = _db(env)
    with db.session() as s:
        job = s.get(Job, args.job_id)
        if job is None:
            print(f"No job #{args.job_id}. Use `jobbot jobs` to list jobs.")
            return 1
        print(f"Applying to {job.title} @ {job.company}\n  {job.apply_url or job.url}")

    def show(_run_id: int, step: dict) -> None:
        kind, text = step.get("kind"), step.get("text", "")
        if kind == "tool":
            print(f"  → {text} {step.get('detail', '')}")
        elif kind in ("thinking", "note"):
            print(textwrap.indent(textwrap.shorten(text, 220), "    · "))
        elif kind == "result" and step.get("error"):
            print(f"    ! {textwrap.shorten(text, 200)}")
        elif kind in ("finish", "nav", "user", "check"):
            print(f"  {text}")

    async def run() -> int:
        manager = ApplyManager(db, env, headless=not args.headed, on_step=show)
        try:
            run_id = await manager.enqueue(
                args.job_id,
                mode="submit" if args.submit and not args.quick else "review",  # Quick fill always asks first
                trigger="cli",
                engine="quick" if args.quick else "agent",
            )
            await manager.wait(run_id)
            while True:
                with db.session() as s:
                    run = s.get(ApplicationRun, run_id)
                    status, summary, error = run.status, run.summary, run.error
                    answers, missing = list(run.answers or []), list(run.missing_info or [])
                print(f"\nStatus: {status}")
                if summary:
                    print(f"Summary: {summary}")
                if error:
                    print(f"Error: {error}")
                if status == "ready_for_review":
                    print("\nWhat's in the form:")
                    for a in answers:
                        print(f"  {a['label'][:70]}: {textwrap.shorten(a['value'] or '(empty)', 90)}")
                    prompt = "Submit it? [y]es / [n]o" + (
                        "" if args.quick else " / or type changes for the agent"
                    )
                    choice = (await asyncio.to_thread(input, f"\n{prompt}: ")).strip()
                    if choice.lower() in ("y", "yes"):
                        run_id = await manager.approve(run_id)
                    elif args.quick or choice.lower() in ("", "n", "no"):
                        await manager.cancel(run_id)
                        print("Not submitted.")
                        return 0
                    else:
                        await manager.revise(run_id, choice)
                    await manager.wait(run_id)
                    continue
                if status == "needs_input":
                    replies = {}
                    for question in missing:
                        replies[question] = await asyncio.to_thread(input, f"{question}\n> ")
                    await manager.provide_answers(run_id, replies, remember=True)
                    await manager.wait(run_id)
                    continue
                if status == "blocked" and args.headed:
                    await asyncio.to_thread(
                        input, "Finish in the browser window, then press Enter to close it."
                    )
                return 0 if status in ("submitted", "ready_for_review") else 1
        finally:
            await manager.shutdown()

    return asyncio.run(run())


def cmd_publish(args: argparse.Namespace, env: Env) -> int:
    import os
    from pathlib import Path

    from jobbot.publish import PublishError, publish

    if not args.verbose:
        # The workflow log of a public repository is public, and library messages can include
        # search terms or company names. Problems are shown on the (encrypted) board instead.
        logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(
            publish(
                env,
                Path(args.out),
                Path(args.state),
                os.environ.get("JOBBOT_CONFIG"),
                os.environ.get("JOBBOT_PAGE_PASSWORD"),
                board_url=os.environ.get("JOBBOT_BOARD_URL", ""),
            )
        )
    except PublishError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_export_config(args: argparse.Namespace, env: Env) -> int:
    import json

    from jobbot.publish import GITHUB_SECRET_LIMIT, encode_config, export_config

    config = export_config(_db(env))
    text = json.dumps(config, indent=2, ensure_ascii=False) if args.json else encode_config(config)
    print(text)
    if len(text.encode()) > GITHUB_SECRET_LIMIT:
        print(
            f"Warning: this is {len(text.encode()) // 1024} KB, over GitHub's 48 KB limit for secrets. "
            "Shorten the resume text or drop --json.",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="jobbot", description="Find jobs you can realistically land, and apply faster."
    )
    parser.add_argument("--version", action="version", version=f"jobbot {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="run the web app and scheduled scans (default)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-scheduler", action="store_true", help="don't run scans automatically")

    sub.add_parser("scan", help="run one scan now and print the results")

    p = sub.add_parser("jobs", help="list jobs")
    p.add_argument("--status", default="new", help="new, saved, applied, ... or all")
    p.add_argument("--min-score", type=int, default=0)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser(
        "add-company", help="watch a company's job board (Greenhouse, Lever, Ashby, Workday, ...)"
    )
    p.add_argument("url", help="careers page or job board URL")
    p.add_argument("--name", default="")

    p = sub.add_parser("check-company", help="fetch a job board and show what jobbot finds, without saving")
    p.add_argument("url")

    p = sub.add_parser("add-search", help="add a job-board keyword search")
    p.add_argument("keywords")
    p.add_argument("--location", default="")
    p.add_argument("--remote", action="store_true")
    p.add_argument("--sources", default="", help="comma separated, e.g. linkedin,indeed,adzuna")

    p = sub.add_parser("apply", help="fill in a job's application (review first unless --submit)")
    p.add_argument("job_id", type=int)
    p.add_argument(
        "--quick", action="store_true", help="Quick fill: no AI and no API key; always asks before submitting"
    )
    p.add_argument("--submit", action="store_true", help="let the agent submit without asking")
    p.add_argument("--headed", action="store_true", help="show the browser window")

    p = sub.add_parser(
        "publish",
        help="scan and write the encrypted job board for GitHub Pages "
        "(reads JOBBOT_CONFIG and JOBBOT_PAGE_PASSWORD from the environment)",
    )
    p.add_argument("--out", default="site", help="directory to write the site to")
    p.add_argument(
        "--state", default=".jobbot-state/state.json", help="encrypted state file to restore and save"
    )
    p.add_argument("--verbose", action="store_true", help="show library logs (may include search terms)")

    p = sub.add_parser(
        "export-config", help="print your setup for the JOBBOT_CONFIG secret of the hosted board"
    )
    p.add_argument("--json", action="store_true", help="print readable JSON instead of the compact form")

    args = parser.parse_args(argv)
    env = get_env()
    logging.basicConfig(level=env.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    command = args.command or "serve"
    if command == "serve" and args.command is None:
        args = parser.parse_args(["serve"])
    handlers = {
        "serve": cmd_serve,
        "scan": cmd_scan,
        "jobs": cmd_jobs,
        "add-company": cmd_add_company,
        "check-company": cmd_check_company,
        "add-search": cmd_add_search,
        "apply": cmd_apply,
        "publish": cmd_publish,
        "export-config": cmd_export_config,
    }
    sys.exit(handlers[command](args, env))

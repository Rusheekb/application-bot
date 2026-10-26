# jobbot

A self-hosted job search assistant. It watches company career pages and job boards, keeps the
postings that fit your background, tells you honestly how likely you are to get an interview for
each one, and can fill in applications for you with an AI agent that waits for your approval
before submitting.

The goal is fewer, better applications: roles you can realistically land, found early, applied
to quickly.

## What it does

- **Finds jobs** on company career sites (Greenhouse, Lever, Ashby, Workday, SmartRecruiters,
  Workable, Recruitee), on LinkedIn, Indeed, Glassdoor, ZipRecruiter and Google Jobs (via
  [JobSpy](https://github.com/speedyapply/JobSpy)), and through the Adzuna, JSearch, Remotive and
  Remote OK APIs. Scans run on a schedule while jobbot is running.
- **De-duplicates** the same job seen in several places and keeps the richest copy, preferring the
  company's own job board and its direct application link.
- **Filters out what isn't realistic**, keeping filtered jobs visible under *Hidden* with the
  reason: titles you don't target, too many years of experience required, "can't sponsor visas"
  (if you need sponsorship), active security clearances, pay below your floor, wrong locations,
  stale postings, excluded companies.
- **Scores each job against your profile.** Without an API key you get a transparent estimate
  (title, skills, experience, freshness, workplace). With Claude, each promising job gets a
  calibrated read on your interview chances: strengths, gaps, dealbreakers, and what to
  emphasize when you apply.
- **A review queue for triage**: sort by best match, save, dismiss or mark applied (keyboard
  shortcuts included), keep notes, and track jobs through interviewing, offer and rejected.
- **Drafts tailored cover letters** from your resume and the posting.
- **Auto-apply (optional)**: an agent opens the application in a real browser and fills it in
  from your profile, uploads your resume, answers screening questions from your saved answers,
  and **stops for your review** before submitting (or submits on its own if you choose).
- **Notifications** to Discord, Slack, ntfy or any webhook when strong matches appear.
- **A free hosted job board (optional)**: GitHub Actions scans on a schedule and publishes your
  matches to an encrypted page on GitHub Pages, so you can review jobs from your phone without
  keeping a computer on.

## Quick start

Requires Python 3.11+.

```bash
git clone https://github.com/Rusheekb/application-bot.git
cd application-bot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"            # core + JobSpy scrapers + browser automation
python -m playwright install chromium
cp .env.example .env               # then add your ANTHROPIC_API_KEY (optional but recommended)
jobbot serve
```

Open http://localhost:8000 and:

1. **Profile**: upload your resume, click *Fill in my profile from this resume*, then check the
   result. Set **target titles** to roles you'd realistically get interviews for (your current
   level, maybe one step up), plus your years of experience, skills, work authorization and
   salary expectations.
2. **Sources**: paste the careers pages of companies you'd like to work for (any page that links
   to their Greenhouse, Lever, Ashby, Workday... board works), and add a couple of focused
   keyword searches for LinkedIn/Indeed.
3. Press **Scan now**. New matches show up on the **Jobs** page, best first.

Minimal install without the optional extras: `pip install -e .` (company boards and API sources
only; no LinkedIn/Indeed scraping, no auto-apply).

## Hosted job board (GitHub Pages)

If you'd rather not keep a computer running, the *Job board* workflow
(`.github/workflows/pages.yml`) scans every 6 hours on GitHub's servers and publishes your
matches as a page on GitHub Pages. It costs nothing: Actions and Pages are free for public
repositories, and without an Anthropic API key jobs get the built-in estimated score.

- **Private, even though Pages sites are public.** The page is encrypted with a passphrase only you
  know (AES-256-GCM, key derived with PBKDF2-SHA256, 600,000 iterations) and decrypted in your
  browser. The job history kept between runs (in the Actions cache) is encrypted the same way, and
  the workflow log, which anyone can read on a public repository, only shows counts.
- **Same engine as the app**: the same sources, filters and scoring, with the best matches first,
  a *Hidden by filters* tab, and New badges for jobs found since your last visit.
- **Triage in the browser**: save, mark applied or dismiss (keyboard shortcuts work). These choices
  are stored in that browser only. Cover letters, notes and auto-apply stay in the local app.

Setup:

1. In the repository on GitHub, open **Settings → Pages** and set **Source** to **GitHub Actions**.
2. Write your config, either:
   - from the local app: **Settings → Hosted job board → Copy config** (or `jobbot export-config`), or
   - by hand, starting from [`examples/jobbot-config.example.json`](examples/jobbot-config.example.json).
     Companies can be any careers-page or job-board URL.
3. In **Settings → Secrets and variables → Actions**, add two repository secrets:
   - `JOBBOT_CONFIG`: the config from step 2 (the compact `jobbot1:...` text or plain JSON).
   - `JOBBOT_PAGE_PASSWORD`: a long passphrase, at least 12 characters (four or five random words
     work well). Anyone can download the encrypted page and try passphrases offline, so don't
     reuse a short password.

   Optional: `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` (free), `JSEARCH_API_KEY` (free tier), and
   `ANTHROPIC_API_KEY` (paid) for Claude's read on your interview chances.
4. Open **Actions → Job board → Run workflow**. When it finishes, your board is at
   `https://<your-username>.github.io/<repository>/`. Unlock it with your passphrase; tick
   *Remember on this device* on your own phone or laptop to skip that next time.

Good to know:

- To change what it looks for, update the `JOBBOT_CONFIG` secret. The next run picks it up and
  re-applies your filters to the jobs it already found.
- Change the schedule with the `cron` line in `.github/workflows/pages.yml`.
- Changing the passphrase starts a new job history (the old one can't be decrypted anymore).
- LinkedIn and Indeed often block requests from cloud servers like GitHub's. Company job boards,
  Remotive, Remote OK, Adzuna and JSearch are more dependable there. Any problems are listed at
  the top of the board.
- A webhook in `settings.notify` works here too; its messages link straight to the postings.
- GitHub pauses scheduled workflows in public repositories after 60 days without any commits.
  If the board stops updating, re-enable the workflow from the Actions tab.
- If the workflow's *deploy* job fails within a second or two, GitHub refused the deployment:
  check that **Settings → Pages → Source** is **GitHub Actions**, and that **Settings →
  Environments → github-pages → Deployment branches and tags** allows the branch the workflow
  runs on (your default branch).

## How it finds jobs

| Source | How | Notes |
| --- | --- | --- |
| Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee | Public job-board APIs | Freshest data, full descriptions, direct application links. Best place to start. |
| Workday | The JSON endpoints the career site itself uses | Searched by your target titles, since these boards can hold thousands of jobs. |
| LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs | [JobSpy](https://github.com/speedyapply/JobSpy) scraper | These sites don't offer job-search APIs and their terms prohibit scraping; they rate-limit aggressively. Keep searches few and focused, and expect occasional failures. |
| Adzuna | Official API (free key) | Descriptions are short snippets. |
| JSearch (RapidAPI) | Official API (free tier) | Google for Jobs results, which cover LinkedIn, Indeed, Glassdoor and company sites, with direct apply links when available. |
| Remotive, Remote OK | Public APIs | Remote roles only. Both ask you to credit them as the source. |

Add a company by pasting almost any careers URL: jobbot recognizes ATS links directly
(`boards.greenhouse.io/acme`, `jobs.lever.co/acme`, `acme.wd5.myworkdayjobs.com/External`, ...)
and otherwise reads the page to find the job board it links to or embeds. Use **Check** next to
a company, or `jobbot check-company <url>`, to confirm what jobbot sees on its board.

When a job disappears from a company's board, it's marked **Closed**.

## How matching works

1. **Relevance**: jobs from company boards are kept only if the title matches one of your target
   titles, search keywords, or the extra titles in *Filters* (word order and common variants like
   "Back-end" / "Backend" or "ML" / "Machine learning" don't matter).
2. **Hard filters** hide jobs with a stated reason (see *Sources → Filters*). Nothing is deleted;
   check the *Hidden* tab if something seems missing.
3. **Estimated score** (0–100): title match (30), skills mentioned in the posting (35), years of
   experience vs. what's asked (20), freshness (10), workplace preference (5).
4. **AI analysis** (with an Anthropic API key): after each scan, the best new jobs (up to 25 by
   default, highest estimated score first) are sent to Claude together with your profile and
   resume. Claude rates your interview chances on a calibrated scale and lists strengths, gaps,
   dealbreakers and tips. You can run it on any job from its page.

## Auto-apply

Turn it on in **Settings → Auto-apply**, then use **Fill application for review** on a job.

- The agent drives a Chromium browser with a small set of tools: read the form, fill fields,
  click, list options, take a screenshot, submit and finish. It works from a structured view of
  the page (fields, options, buttons, alerts and a text excerpt) rather than raw HTML.
- **Review mode (default):** it fills everything in and stops. You see every answer (AI-written
  answers are flagged) and a screenshot, then click **Submit application**, or ask it for
  changes in plain words. The paused browser stays open for 30 minutes by default; after that,
  approving re-runs the agent with the answers you reviewed.
- In review mode the agent can't submit: the submit button, and any other button that might
  send a filled-in form (an "Apply" or "Done" button, or one it doesn't recognize), is blocked,
  every click is checked against the page as it is at that moment, and the page's own scripts
  can't submit the form either. Changes you ask for after approving need your approval again.
- **Submit mode:** it submits once every required field is filled in, and still stops when it's
  unsure.
- If it needs information you haven't given (say, a question about a certification), it fills in
  everything else and asks you. Your answers can be saved to your profile for next time.
- **It stops and hands the application back to you** on CAPTCHAs (it never tries to solve them),
  sign-in or account-creation walls, and requests for passwords, government ID numbers, bank
  details or dates of birth.
- It uses only your profile and resume and **never invents experience**. Voluntary
  self-identification questions default to "decline to self-identify" unless you fill them in.
- Optionally, it can run automatically after each scan for jobs above an AI-score threshold, with
  a daily cap.

Works best on Greenhouse, Lever, Ashby, Workable, SmartRecruiters and Recruitee forms. Workday,
iCIMS, Taleo, LinkedIn Easy Apply and Indeed require accounts, so the agent will usually hand
those back. Some sites use invisible bot detection that can reject automated submissions; when
that happens, open the application from the run page and submit it yourself. Turning off
*headless* in Settings shows the browser window so you can watch and take over (needs a display).

**Tip:** quality beats volume. A few well-matched applications sent within days of posting tend
to get more responses than dozens of untargeted ones.

## Command line

```bash
jobbot serve [--host H] [--port P] [--no-scheduler]   # web UI + scheduled scans (default command)
jobbot scan                                           # run one scan now and print the results
jobbot jobs [--status new|saved|applied|...|all] [--min-score N]
jobbot add-company https://jobs.lever.co/acme [--name Acme]
jobbot check-company https://boards.greenhouse.io/acme   # fetch and preview, saves nothing
jobbot add-search "backend engineer" --location "New York, NY" --sources linkedin,indeed
jobbot apply JOB_ID [--submit] [--headed]             # run the apply agent in the terminal
jobbot export-config [--json]                         # your setup, for the hosted board's JOBBOT_CONFIG
jobbot publish [--out site] [--state FILE]            # scan and write the encrypted board (used by the workflow)
```

To scan on a schedule without keeping the web UI running, call `jobbot scan` from cron or a
systemd timer instead.

## Docker

```bash
cp .env.example .env    # add your keys
docker compose up -d --build
```

Data lives in `./data`. The compose file publishes the UI on `127.0.0.1:8000` only. If you expose
it to other machines, set `JOBBOT_PASSWORD` and add the hostname to `JOBBOT_ALLOWED_HOSTS`. The
container runs the browser headless.

## Costs

The app and the hosted job board are free, and both work without an Anthropic API key. Claude
usage is billed by Anthropic per token, and the defaults keep it modest:

- Fit analysis uses `claude-opus-5-5` at low effort, caches your profile and resume across all
  jobs in a scan, and is capped at 25 jobs per scan. Each scan's approximate cost appears in
  *Scan history*. For the lowest cost, switch the model to Claude Haiku 5.5 in Settings.
- An apply-agent run usually takes a handful of model turns; each run shows its approximate cost.
- If Claude's safety filters ever decline a request, jobbot retries it on a fallback model
  (server-side refusal fallbacks). Turn this off in Settings if your account or platform doesn't
  support it; jobbot also turns it off automatically if the API rejects it.

## Privacy and security

- Everything is stored locally in `JOBBOT_DATA_DIR`: a SQLite database, your resume and
  screenshots. The hosted board keeps your config in GitHub secrets and everything else encrypted
  (see above).
- Fit analysis sends Claude your profile summary and resume, but not your contact details. The
  apply agent sends the details it needs to fill in forms.
- The web UI binds to localhost by default and rejects requests for unknown hostnames (DNS
  rebinding) and cross-site form posts (CSRF). Set `JOBBOT_PASSWORD` before exposing it anywhere.
- Job postings and application pages are treated as untrusted input: links are limited to
  http(s), descriptions are rendered as plain text, and the agent is instructed to ignore
  instructions embedded in pages.

## Development

```bash
pip install -e ".[dev,all]"
python -m playwright install chromium
pytest            # unit, integration and real-browser tests
ruff check src tests && ruff format --check src tests
```

The source connectors are tested against fixture responses in each API's documented format
(`tests/fixtures/api`). The apply agent is tested end to end in Chromium against local copies of
Greenhouse-, Lever- and Ashby-style forms, with a scripted stand-in for Claude (`tests/fakes.py`).
The Claude integration tests run the real Anthropic SDK against a mocked HTTP transport to check
the exact requests sent.

### Layout

```
src/jobbot/
  sources/      job board and ATS connectors, careers-page detection
  pipeline.py   scan: fetch → de-duplicate → filter → score → store → notify
  matching.py   relevance, hard filters, estimated score
  ai/           Claude client, fit analysis, resume parsing, cover letters
  apply/        auto-apply agent (snapshot.js, browser.py, agent.py, manager.py)
  web/          FastAPI app, templates, static assets
  publish.py    hosted job board: config sync, encryption, static page (board/)
  cli.py        command line
```

## Limitations

- Job boards change their APIs and page structures without notice; a connector may need
  updating when that happens. Errors show up per source in *Scan history* without stopping the
  rest of the scan.
- Scraping LinkedIn, Indeed and similar sites is against their terms of service and may get
  rate-limited or blocked. The official APIs (Adzuna, JSearch) are the safer alternative.
- The heuristic score is a rough guide; the AI analysis is better but can still be wrong. Use it
  to prioritize, not as a verdict.

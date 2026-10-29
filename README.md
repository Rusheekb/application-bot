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
- **Auto-apply (optional)**: opens the application in a real browser, fills it in from your
  profile, uploads your resume, answers screening questions from your saved answers, and **stops
  for your review** before submitting. **Quick fill** does this with plain matching rules and needs
  no API key; with a Claude key, the **agent** can also write answers to open-ended questions and
  find its way through unusual forms (and can submit on its own if you choose).
- **Notifications** to Discord, Slack, ntfy or any webhook when strong matches appear.
- **A free public job board (optional)**: GitHub Actions scans a shared list of sources on a
  schedule and publishes a page on GitHub Pages where each visitor tailors the ranking to
  themselves, with their settings and their own optional Anthropic API key kept in their browser.

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

A public job board that anyone can open, with no account and no passphrase. Like a calculator
app, it runs entirely in each visitor's browser, so every visitor tailors it to themselves and
nobody sees anyone else's settings. It costs nothing to run: GitHub Actions and Pages are free for
public repositories.

- **Shared job feed.** The *Job board* workflow (`.github/workflows/pages.yml`) scans the
  companies and job-site searches in [`feed/sources.json`](feed/sources.json) every 6 hours and
  publishes the results as `jobs.json`, next to the page. It holds only public job postings.
- **Personal settings, kept in the browser.** Target titles, experience, skills, locations,
  companies, filters and resume text are saved in the visitor's browser (localStorage), along
  with which jobs they saved, applied to or dismissed. The page ranks the feed with the same
  filters and estimated score as the app. Nothing a visitor enters is sent to the site, which has
  no server or database.
- **Optional: Claude with your own API key.** A visitor can paste their own Anthropic API key in
  Settings. Claude then reads postings against their resume and rates their chances of getting an
  interview (one job at a time, or the top 5–25 on a tab), and can fill in their settings from
  their resume. Requests go straight from the browser to `api.anthropic.com` and are billed to
  the visitor's own account: with Claude Haiku 5.5, about a tenth of a cent per job.

How the API key is protected:

- It's kept for the current tab only (sessionStorage) unless the visitor ticks *Remember the key
  on this device*. It's never in the settings backup, and *Remove it* deletes it.
- It's only sent to `api.anthropic.com`, in the `x-api-key` header, from the visitor's browser.
  The page's Content Security Policy allows connections only to the site itself and Anthropic's
  API, and allows no scripts but the site's own (no inline scripts, no CDNs or analytics), so no
  third-party code can read the key. Job postings are shown as plain text, never as HTML, and the
  page refuses to run inside another site's frame.
- Claude is told to treat postings and resumes as untrusted data, and its answers are only
  displayed, never acted on.
- The page recommends a separate key with a monthly spend limit (Anthropic Console → Limits),
  confirms before analyzing several jobs at once, and shows a running cost estimate.

Setup:

1. In the repository on GitHub, open **Settings → Pages** and set **Source** to **GitHub Actions**.
2. Edit [`feed/sources.json`](feed/sources.json) to list the companies (any careers-page or
   job-board URL) and job-site searches everyone's board should draw from. Only job titles that
   match `keep_titles`, or a search's keywords, are kept from company boards.
3. Open **Actions → Job board → Run workflow** (it also runs when `feed/sources.json` changes on
   `main`). When it finishes, the board is at `https://<your-username>.github.io/<repository>/`.
4. Open the board and fill in **Settings**. To bring over a setup from the local app, copy it
   from the app's **Settings → Hosted job board** (or `jobbot export-config`) and paste it under
   *Back up or import*.

Good to know:

- Settings live in one browser. To use another device, copy them from *Back up or import* and
  paste them there. Clearing the site's data in the browser erases them, as does *Erase everything
  on this device*.
- Optional repository secrets `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` (free) and `JSEARCH_API_KEY` (free
  tier) add those job sources to the shared feed. They stay on GitHub's servers.
- Visitors can suggest companies with the link at the bottom of the board (it opens a GitHub
  issue); add them to `feed/sources.json`.
- Notifications and auto-apply need a server, so they stay in the local app.
- LinkedIn and Indeed often block requests from cloud servers like GitHub's. Company job boards,
  Remotive and Remote OK are more dependable there. Problems are listed at the top of the board.
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

Turn it on in **Settings → Auto-apply**, then open a job and choose **Quick fill** (no API key
needed) or, with an Anthropic API key, **Fill application for review** (the agent).

### Quick fill (free, no API key)

Quick fill reads the form the same way the agent does, then fills in what your profile covers:
name, contact details, links, location, education, current job, resume and cover letter uploads,
work authorization and sponsorship, voluntary self-identification (declined unless your profile
says otherwise), "how did you hear about us", salary, notice period and start date, plus anything
under **Profile → Saved answers**.

- Required questions it can't answer become questions for you. Your answers can be saved, so the
  next application with the same question fills itself.
- It answers eligibility questions only from what your profile states: with no work
  authorization filled in, or for a different country, it asks you instead of guessing.
- It leaves open-ended questions ("Why do you want to work here?") and skill-specific ones
  ("Years of experience with Spark") for you, and can't follow written instructions.
- It always stops for your review; **Submit application** then clicks the form's submit button.
  The same safeguards apply as for the agent.
- From the terminal: `jobbot apply JOB_ID --quick`.

### The agent (Claude API key)

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
jobbot apply JOB_ID [--quick] [--submit] [--headed]   # fill an application (--quick: no AI, no key)
jobbot export-config [--json]                         # your setup, to import on the hosted board
jobbot publish [--sources FILE] [--out site]         # scan the shared sources and write the public board
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

The app and the hosted job board are free, and both work without an Anthropic API key: you get
job finding, filtering, the estimated score, the review queue, notifications and Quick fill
(notifications and Quick fill in the app). On the hosted board, each visitor who adds their own
key pays for their own Claude requests.
A key adds Claude's fit analysis, resume parsing, cover letters and the apply agent. Note that a
Claude.ai subscription doesn't include API access; the API is billed separately, per token, and
the defaults keep it modest:

- Fit analysis uses `claude-opus-5-5` at low effort, caches your profile and resume across all
  jobs in a scan, and is capped at 25 jobs per scan. Each scan's approximate cost appears in
  *Scan history*. For the lowest cost, switch the model to Claude Haiku 5.5 in Settings.
- An apply-agent run usually takes a handful of model turns; each run shows its approximate cost.
- If Claude's safety filters ever decline a request, jobbot retries it on a fallback model
  (server-side refusal fallbacks). Turn this off in Settings if your account or platform doesn't
  support it; jobbot also turns it off automatically if the API rejects it.

## Privacy and security

- Everything is stored locally in `JOBBOT_DATA_DIR`: a SQLite database, your resume and
  screenshots. The hosted board keeps each visitor's settings and optional API key in their own
  browser and publishes only public job postings (see above).
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
  publish.py    hosted job board: shared feed scan and site (board/: page, match.js, claude.js)
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

// The public job board. Jobs come from jobs.json, a shared feed the scan publishes; everything
// personal (settings, scores, saved jobs, an optional Anthropic API key) stays in this browser.
import { band, estimate, hiddenReason, isRelevant, relevanceTerms, WORKPLACES } from "./match.js?v=__VERSION__";
import { DEFAULT_MODEL, MODELS, analyzeFit, candidateBlock, cost, estimateCost, jobBlock, looksLikeKey, suggestFromResume, testKey } from "./claude.js?v=__VERSION__";

const PAGE = 100;
const MAX_FITS = 1000;
const LIKELIHOOD = { high: "High chance", medium: "Decent chance", low: "Long shot", very_low: "Very unlikely" };
const SENIORITY = { under: "role is above your level", match: "good match", over: "you may be overqualified", unclear: "unclear" };
const TABS = [["review", "To review"], ["saved", "Saved"], ["applied", "Applied"], ["dismissed", "Dismissed"], ["hidden", "Hidden by filters"], ["all", "All"]];
const KEYS_URL = "https://platform.claude.com/settings/keys";
const LIMITS_URL = "https://platform.claude.com/settings/limits";

// --- storage: everything under "jobbot." in this browser ------------------------------------------
function storage(get) {
  return {
    get(key, fallback) { try { const v = get().getItem(`jobbot.${key}`); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; } },
    set(key, value) { try { get().setItem(`jobbot.${key}`, JSON.stringify(value)); return true; } catch (e) { return false; } },
    remove(key) { try { get().removeItem(`jobbot.${key}`); } catch (e) { /* storage unavailable */ } },
    clear() { try { const s = get(); Object.keys(s).filter((k) => k.startsWith("jobbot.")).forEach((k) => s.removeItem(k)); } catch (e) { /* ignore */ } },
  };
}
const local = storage(() => window.localStorage);
const session = storage(() => window.sessionStorage);

// The earlier passphrase-protected board kept its data under "jobbot:<what>:<site>". Keep the
// saved/applied marks, and delete the rest (including its stored key and GitHub token).
function migrateOldBoard() {
  try {
    const old = Object.keys(localStorage).filter((k) => k.startsWith("jobbot:"));
    if (!old.length) return;
    let marks = {};
    const seenIds = new Set(local.get("seen", []));
    for (const key of old) {
      const value = JSON.parse(localStorage.getItem(key) || "null");
      if (key.startsWith("jobbot:status:") && value && typeof value === "object") marks = { ...marks, ...value };
      if (key.startsWith("jobbot:seen:") && Array.isArray(value)) value.forEach((id) => seenIds.add(id));
      localStorage.removeItem(key);
    }
    local.set("status", { ...marks, ...local.get("status", {}) });
    local.set("seen", [...seenIds].slice(-5000));
  } catch (e) { /* storage unavailable */ }
}

// --- your settings ---------------------------------------------------------------------------------
// The same shape as the jobbot app's export, so a config copied from it can be imported here.
const DEFAULT_DOC = {
  profile: {
    target_titles: [], years_experience: null, skills: [], current_title: "", workplace_preferences: [...WORKPLACES],
    work_authorization: "", requires_sponsorship: false, security_clearance: "", salary_currency: "USD",
  },
  settings: {
    filters: {
      title_include: [], title_exclude: [], description_exclude: [], company_include: [], company_exclude: [],
      locations: [], location_exclude: [], include_remote: true, min_salary: null, max_years_gap: 2, max_age_days: 30,
      hide_no_sponsorship: true, hide_clearance_required: true,
    },
    ai: { model: DEFAULT_MODEL },
  },
  resume_text: "",
};
const LISTS = {
  profile: ["target_titles", "skills", "workplace_preferences", "certifications", "languages"],
  filters: ["title_include", "title_exclude", "description_exclude", "company_include", "company_exclude", "locations", "location_exclude"],
};
const clone = (value) => JSON.parse(JSON.stringify(value ?? {}));
const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const numberOrNull = (value) => (value === null || value === undefined || value === "" || !Number.isFinite(Number(value)) ? null : Number(value));

// Fill in defaults and fix types, so settings from anywhere (an old version, an import) are safe to use.
function normalize(raw) {
  const doc = isObject(raw) ? clone(raw) : {};
  doc.profile = { ...DEFAULT_DOC.profile, ...(isObject(doc.profile) ? doc.profile : {}) };
  doc.settings = isObject(doc.settings) ? doc.settings : {};
  const filters = { ...DEFAULT_DOC.settings.filters, ...(isObject(doc.settings.filters) ? doc.settings.filters : {}) };
  doc.settings.filters = filters;
  doc.settings.ai = { ...DEFAULT_DOC.settings.ai, ...(isObject(doc.settings.ai) ? doc.settings.ai : {}) };
  if (!MODELS.some((m) => m.id === doc.settings.ai.model)) doc.settings.ai.model = DEFAULT_MODEL;
  const strings = (value) => (Array.isArray(value) ? value.filter((x) => typeof x === "string") : []);
  for (const name of LISTS.profile) doc.profile[name] = strings(doc.profile[name]);
  for (const name of LISTS.filters) filters[name] = strings(filters[name]);
  doc.profile.years_experience = numberOrNull(doc.profile.years_experience);
  for (const name of ["min_salary", "max_years_gap", "max_age_days"]) filters[name] = numberOrNull(filters[name]);
  for (const name of ["requires_sponsorship", "willing_to_relocate"]) doc.profile[name] = Boolean(doc.profile[name]);
  for (const name of ["include_remote", "hide_no_sponsorship", "hide_clearance_required"]) filters[name] = Boolean(filters[name]);
  for (const name of ["current_title", "work_authorization", "security_clearance", "summary", "salary_currency"]) {
    if (typeof doc.profile[name] !== "string") doc.profile[name] = DEFAULT_DOC.profile[name] ?? "";
  }
  doc.resume_text = typeof doc.resume_text === "string" ? doc.resume_text : "";
  return doc;
}

// --- your Anthropic API key --------------------------------------------------------------------------
// Kept in this tab only (sessionStorage) unless you choose to remember it on this device.
const KEY = "anthropic-key";
const savedKey = () => session.get(KEY, null) || local.get(KEY, null);
const keyRemembered = () => Boolean(local.get(KEY, null));
function storeKey(key, remember) {
  session.remove(KEY);
  local.remove(KEY);
  if (key) (remember ? local : session).set(KEY, key);
}

// --- helpers ---------------------------------------------------------------------------------------
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false || child === "") continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
const $ = (id) => document.getElementById(id);
const safeUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u) ? u : null);
const external = (href, text) => el("a", { href, target: "_blank", rel: "noopener noreferrer" }, text);
const plural = (n, word, many = `${word}s`) => `${n.toLocaleString("en-US")} ${n === 1 ? word : many}`;
const money = (dollars) => (dollars < 0.01 ? "less than a cent" : `about $${dollars.toFixed(2)}`);
function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  const d = Math.round(s / 86400);
  return d < 60 ? `${d}d ago` : `${Math.round(d / 30)}mo ago`;
}
const time = (iso) => (iso ? Date.parse(iso) || 0 : 0);

// --- state -----------------------------------------------------------------------------------------
migrateOldBoard();
let feed = null;
let doc = normalize(local.get("settings", null));
const firstVisit = local.get("settings", null) === null;
let statuses = local.get("status", {});
let fits = local.get("fits", {});
const seenBefore = new Set(local.get("seen", []));
let view = "jobs", tab = "review", selected = -1, shown = PAGE;
const expanded = new Set();
const jobErrors = new Map();
const analyzing = new Set();
let computed = { visible: [], hidden: [], irrelevant: 0, terms: [] };
let batch = null;  // the running "analyze the top N" batch

function compute() {
  const terms = relevanceTerms(doc);
  const now = Date.now();
  const visible = [], hidden = [];
  let irrelevant = 0;
  for (const job of feed.jobs) {
    if (!isRelevant(job.title, terms)) { irrelevant += 1; continue; }
    const reason = hiddenReason(job, doc, now);
    if (reason) hidden.push({ job, reason });
    else visible.push({ job, est: estimate(job, doc, terms, now) });
  }
  computed = { visible, hidden, irrelevant, terms };
}

const fitOf = (job) => fits[job.id] || null;
const scoreOf = (item) => (fitOf(item.job) ? fitOf(item.job).score : item.est.score);
const statusOf = (job) => statuses[job.id] || "review";

function saveFit(job, fit) {
  fits[job.id] = fit;
  const ids = Object.keys(fits);
  if (ids.length > MAX_FITS) ids.sort((a, b) => time(fits[a].at) - time(fits[b].at)).slice(0, ids.length - MAX_FITS).forEach((id) => delete fits[id]);
  local.set("fits", fits);
}
function recordUsage(usage, model) {
  const total = local.get("usage", { calls: 0, dollars: 0 });
  local.set("usage", { calls: total.calls + 1, dollars: total.dollars + cost(usage, model) });
}

// --- start -----------------------------------------------------------------------------------------
async function main() {
  if (window.top !== window.self) {  // don't run inside someone else's page (clickjacking)
    $("framed").hidden = false;
    return;
  }
  $("app").hidden = false;
  $("list").replaceChildren(el("div", { class: "empty" }, el("span", { class: "spinner" }), " Loading jobs…"));
  try {
    const res = await fetch("jobs.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    feed = await res.json();
    if (!Array.isArray(feed.jobs)) throw new Error("no jobs in the feed");
  } catch (err) {
    $("list").replaceChildren(el("div", { class: "empty" }, `Couldn't load the jobs (${err.message}). Reload the page to try again.`));
    return;
  }
  const updated = $("updated");
  updated.textContent = `Jobs updated ${ago(feed.generated)}`;
  updated.title = new Date(feed.generated).toLocaleString();
  renderErrors();
  renderFooter();
  for (const id of ["search", "min-score", "sort"]) $(id).addEventListener("input", () => { shown = PAGE; render(); });
  $("settings-button").addEventListener("click", () => (view === "settings" ? showJobs() : showSettings()));
  compute();
  if (firstVisit) showSettings(); else showJobs();
  local.set("seen", [...new Set([...seenBefore, ...feed.jobs.map((j) => j.id)])].slice(-5000));
}

function renderErrors() {
  const errors = (feed.scan && feed.scan.errors) || [];
  const box = $("errors");
  box.replaceChildren();
  if (!errors.length) return;
  box.append(el("details", { class: "notice" },
    el("summary", {}, `${plural(errors.length, "source")} had problems in the last update`),
    el("ul", {}, errors.map((e) => el("li", {}, el("strong", {}, e.source), `: ${e.error}`)))));
}

function renderFooter() {
  const src = feed.sources || { companies: 0, searches: [] };
  const repo = typeof feed.repository === "string" && /^[\w.-]+\/[\w.-]+$/.test(feed.repository) ? feed.repository : null;
  $("footer").replaceChildren(
    el("p", {}, `Jobs come from a shared feed of ${plural(src.companies || 0, "company job board", "company job boards")} and ${plural((src.searches || []).length, "job-site search", "job-site searches")}, refreshed every few hours. `,
      "Your settings, saved jobs and API key stay in this browser: the site has no server or database, and only sends your profile to Anthropic when you ask Claude for an analysis with your own key.",
      repo ? [" Missing a company? ", external(`https://github.com/${repo}/issues/new?title=${encodeURIComponent("Add a company to the job board")}&body=${encodeURIComponent("Careers page or job board URL:\n")}`, "Suggest it"), "."] : null));
}

function showJobs() {
  view = "jobs";
  $("jobs-view").hidden = false;
  $("settings-view").hidden = true;
  $("settings-button").setAttribute("aria-pressed", "false");
  render();
}
function showSettings() {
  view = "settings";
  if (!editor) buildSettings(doc);
  $("jobs-view").hidden = true;
  $("settings-view").hidden = false;
  $("settings-button").setAttribute("aria-pressed", "true");
  window.scrollTo(0, 0);
}

// --- the board -------------------------------------------------------------------------------------
function setStatus(job, status) {
  if (status === "review") delete statuses[job.id]; else statuses[job.id] = status;
  local.set("status", statuses);
  render();
}

function listItems() {
  const q = $("search").value.trim().toLowerCase();
  const min = Number($("min-score").value);
  const sort = $("sort").value;
  let items = tab === "all" ? computed.visible : computed.visible.filter((i) => statusOf(i.job) === tab);
  if (q) items = items.filter((i) => `${i.job.title} ${i.job.company} ${i.job.location}`.toLowerCase().includes(q));
  if (min) items = items.filter((i) => scoreOf(i) >= min);
  const by = {
    best: (a, b) => scoreOf(b) - scoreOf(a) || time(b.job.posted) - time(a.job.posted),
    newest: (a, b) => time(b.job.posted || b.job.found) - time(a.job.posted || a.job.found),
    found: (a, b) => time(b.job.found) - time(a.job.found),
  }[sort];
  return [...items].sort(by);
}

function renderTabs() {
  const counts = { review: 0, saved: 0, applied: 0, dismissed: 0, hidden: computed.hidden.length, all: computed.visible.length };
  for (const i of computed.visible) counts[statusOf(i.job)] = (counts[statusOf(i.job)] || 0) + 1;
  $("tabs").replaceChildren(...TABS.map(([key, label]) =>
    el("button", { class: key === tab ? "active" : "", onclick: () => { tab = key; selected = -1; shown = PAGE; render(); } },
      label, el("span", { class: "count" }, counts[key] || 0))));
}

function card(item, index) {
  const { job, est } = item;
  const fit = fitOf(job);
  const status = statusOf(job);
  const isNew = seenBefore.size > 0 && !seenBefore.has(job.id);
  const score = fit ? fit.score : est.score;
  const workplace = job.workplace && !job.location.toLowerCase().includes(job.workplace) ? job.workplace[0].toUpperCase() + job.workplace.slice(1) : "";
  const metaParts = [job.location, workplace, job.salary, job.posted ? `posted ${ago(job.posted)}` : `found ${ago(job.found)}`].filter(Boolean);
  const summary = fit
    ? el("p", { class: "summary" }, fit.likelihood ? el("strong", {}, `${LIKELIHOOD[fit.likelihood] || ""}. `) : null, fit.summary)
    : el("p", { class: "summary muted" },
        est.skills.length ? `Mentions ${est.skills.slice(0, 6).join(", ")}${est.skills.length > 6 ? ` and ${est.skills.length - 6} more` : ""}. ` : "",
        est.notes.join(". "));
  const act = (label, value, cls) => el("button", { class: `btn-sm ${cls || ""}`, "data-action": value, onclick: () => setStatus(job, value) }, label);
  const open = safeUrl(job.apply || job.url);
  const actions = el("div", { class: "actions" },
    open ? el("a", { class: "btn btn-sm", "data-action": "open", href: open, target: "_blank", rel: "noopener noreferrer" }, "Open ↗") : null,
    status !== "saved" ? act("Save", "saved") : act("Unsave", "review"),
    status !== "applied" ? act("Applied", "applied") : act("Not applied", "review"),
    status !== "dismissed" ? act("Dismiss", "dismissed", "btn-ghost") : act("Restore", "review", "btn-ghost"));
  const node = el("article", { class: `job${index === selected ? " selected" : ""}`, "data-id": job.id },
    el("div", { class: "job-row" },
      el("div", { class: `score ${band(score)}`, title: fit ? "Claude's fit score" : "Estimated fit" }, score, el("small", {}, fit ? "AI" : "est.")),
      el("div", {},
        el("button", { class: "title", "aria-expanded": expanded.has(job.id) ? "true" : "false", onclick: () => toggle(job) }, job.title),
        el("div", { class: "meta" }, el("strong", {}, job.company), metaParts.map((p) => ` · ${p}`)),
        summary,
        el("div", { class: "chips" },
          isNew ? el("span", { class: "chip new" }, "New") : null,
          job.sources.map((s) => el("span", { class: "chip" }, s)),
          job.years ? el("span", { class: "chip" }, `${job.years}+ yrs`) : null,
          status !== "review" && tab === "all" ? el("span", { class: "chip" }, status) : null)),
      actions));
  if (expanded.has(job.id)) node.append(details(item));
  return node;
}

function details(item) {
  const { job, est } = item;
  const fit = fitOf(job);
  const list = (title, items) => (items && items.length ? [el("h3", {}, title), el("ul", {}, items.map((x) => el("li", {}, x)))] : []);
  const bd = est.breakdown;
  const model = (MODELS.find((m) => m.id === (fit ? fit.model : "")) || {}).label || (fit ? fit.model : "");
  const error = jobErrors.get(job.id);
  const busy = analyzing.has(job.id);
  const key = savedKey();
  const analyzeButton = (label) => el("button", { class: "btn-sm", "data-action": "analyze", disabled: busy || !key, onclick: () => analyzeOne(job) }, busy ? "Analyzing…" : label);
  const chances = fit
    ? [el("h3", {}, "Your chances (Claude)"),
       el("p", { class: "m0 small" }, `${LIKELIHOOD[fit.likelihood] || ""}${fit.seniority ? ` · seniority: ${SENIORITY[fit.seniority] || fit.seniority}` : ""}`),
       ...list("Dealbreakers", fit.dealbreakers), ...list("Why you fit", fit.strengths), ...list("Gaps", fit.gaps), ...list("To stand out", fit.tips),
       el("p", { class: "faint small" }, `Analyzed with ${model} ${ago(fit.at)}. `, key ? analyzeButton("Analyze again") : null)]
    : [el("h3", {}, "Estimated fit"),
       el("p", { class: "muted m0 small" }, "From title, skills, experience, freshness and workplace."),
       key ? el("p", { class: "small" }, analyzeButton("Ask Claude about my chances"))
           : el("p", { class: "muted small" }, "Add your own Anthropic API key in ", el("button", { class: "link", onclick: showSettings }, "Settings"), " to have Claude read the posting against your resume.")];
  const side = el("div", {},
    chances,
    error ? el("p", { class: "error", role: "alert" }, error) : null,
    el("h3", {}, "Estimated score"),
    el("dl", { class: "kv" },
      el("dt", {}, "Title"), el("dd", {}, `${bd.title}/30`),
      el("dt", {}, "Skills"), el("dd", {}, `${bd.skills}/35`),
      el("dt", {}, "Experience"), el("dd", {}, `${bd.experience}/20`),
      el("dt", {}, "Freshness"), el("dd", {}, `${bd.freshness}/10`),
      el("dt", {}, "Workplace"), el("dd", {}, `${bd.workplace}/5`)),
    job.url && job.apply && job.url !== job.apply && safeUrl(job.url) ? el("p", { class: "small" }, external(job.url, "View the original posting ↗")) : null);
  return el("div", { class: "details" },
    el("div", {}, el("h3", {}, "Job description"), el("div", { class: "description" }, job.description || "No description available; open the posting to read it.")),
    side);
}

function toggle(job) {
  if (expanded.has(job.id)) expanded.delete(job.id); else expanded.add(job.id);
  render();
}

function hiddenRow({ job, reason }) {
  const open = safeUrl(job.apply || job.url);
  return el("article", { class: "job" },
    el("div", {}, open ? el("a", { class: "title", href: open, target: "_blank", rel: "noopener noreferrer" }, job.title) : el("strong", {}, job.title),
      el("div", { class: "meta" }, el("strong", {}, job.company), job.location ? ` · ${job.location}` : ""),
      el("div", { class: "chips" }, el("span", { class: "chip warn" }, `Hidden: ${reason}`))));
}

function moreButton(total) {
  $("more").replaceChildren(total > shown ? el("button", { onclick: () => { shown += PAGE; render(); } }, `Show more (${(total - shown).toLocaleString("en-US")} left)`) : "");
}

function render() {
  if (!feed) return;
  renderTabs();
  renderAiBar();
  const list = $("list");
  const subtitle = $("subtitle");
  if (tab === "hidden") {
    subtitle.textContent = "Jobs your filters hid, with the reason. Change your filters in Settings.";
    list.replaceChildren(...(computed.hidden.length ? computed.hidden.slice(0, shown).map(hiddenRow) : [el("div", { class: "empty" }, "Nothing hidden.")]));
    list._items = [];
    moreButton(computed.hidden.length);
    return;
  }
  const items = listItems();
  const others = computed.irrelevant
    ? ` ${plural(computed.irrelevant, "other job")} in the feed ${computed.irrelevant === 1 ? "doesn't" : "don't"} match your target titles.`
    : "";
  subtitle.textContent = `${plural(items.length, "job")}, best matches first.${others}`;
  if (selected >= Math.min(items.length, shown)) selected = Math.min(items.length, shown) - 1;
  const empty = !doc.profile.target_titles.length && !computed.visible.length
    ? el("div", { class: "empty" }, "No jobs to show. ", el("button", { class: "link", onclick: showSettings }, "Open Settings"), " to choose what you're looking for.")
    : el("div", { class: "empty" }, tab === "review" ? "You're all caught up. New jobs appear after the next update." : "Nothing here.");
  list.replaceChildren(...(items.length ? items.slice(0, shown).map(card) : [empty]));
  list._items = items.slice(0, shown);
  moreButton(items.length);
}

document.addEventListener("keydown", (e) => {
  if (!feed || view !== "jobs" || e.metaKey || e.ctrlKey || e.altKey || e.target.closest("input, select, textarea")) return;
  if (e.key === "Enter" && e.target.closest("button, a")) return;  // let the focused control handle it
  const items = $("list")._items || [];
  if (!items.length) return;
  const move = (i) => { selected = Math.max(0, Math.min(i, items.length - 1)); render(); const n = document.querySelector(".job.selected"); if (n) n.scrollIntoView({ block: "nearest" }); };
  if (e.key === "j") { move(selected + 1); e.preventDefault(); return; }
  if (e.key === "k") { move(selected - 1); e.preventDefault(); return; }
  const item = items[selected];
  if (!item) return;
  const job = item.job;
  if (e.key === "Enter") { toggle(job); e.preventDefault(); }
  else if (e.key === "s") setStatus(job, statusOf(job) === "saved" ? "review" : "saved");
  else if (e.key === "a") setStatus(job, statusOf(job) === "applied" ? "review" : "applied");
  else if (e.key === "d") setStatus(job, statusOf(job) === "dismissed" ? "review" : "dismissed");
  else if (e.key === "o") { const u = safeUrl(job.apply || job.url); if (u) window.open(u, "_blank", "noopener"); }
});

// --- Claude (optional, with your own key) ----------------------------------------------------------
const model = () => doc.settings.ai.model;
const modelLabel = () => (MODELS.find((m) => m.id === model()) || MODELS[0]).label;

async function analyzeOne(job) {
  const key = savedKey();
  if (!key || analyzing.has(job.id)) return;
  analyzing.add(job.id);
  jobErrors.delete(job.id);
  render();
  try {
    const [fit, usage] = await analyzeFit(key, model(), doc, job);
    saveFit(job, fit);
    recordUsage(usage, model());
  } catch (err) {
    jobErrors.set(job.id, err.message || String(err));
  } finally {
    analyzing.delete(job.id);
    render();
  }
}

function renderAiBar() {
  const bar = $("ai-bar");
  if (tab === "hidden" || tab === "dismissed") { bar.replaceChildren(); return; }
  if (!savedKey()) {
    bar.replaceChildren("Optional: add your own Anthropic API key in ", el("button", { class: "link", onclick: showSettings }, "Settings"), " to have Claude rate your chances for each job.");
    return;
  }
  if (batch) {
    bar.replaceChildren(...(batch.running
      ? [el("span", { class: "spinner" }), ` Claude is analyzing ${batch.done + 1} of ${batch.items.length}… `, el("button", { class: "btn-sm", onclick: () => batch.controller.abort() }, "Stop")]
      : batch.confirm
        ? [`Send ${plural(batch.items.length, "job posting")} and your profile and resume to Anthropic? ${money(batch.estimate)} with ${modelLabel()}, billed to your key. `,
           el("button", { class: "btn-sm btn-primary", "data-action": "confirm-batch", onclick: runBatch }, "Analyze"), " ",
           el("button", { class: "btn-sm btn-ghost", onclick: () => { batch = null; render(); } }, "Cancel")]
        : [batch.message, " ", el("button", { class: "btn-sm btn-ghost", onclick: () => { batch = null; render(); } }, "OK")]));
    return;
  }
  const count = el("select", { "aria-label": "How many jobs" }, [5, 10, 25].map((n) => el("option", { value: n, selected: n === 10 }, n)));
  bar.replaceChildren(`${modelLabel()}: `,
    el("button", { class: "btn-sm", "data-action": "batch", onclick: () => startBatch(Number(count.value)) }, "Analyze the top"), count,
    " jobs on this tab that it hasn't read yet");
}

function startBatch(count) {
  const items = (listItems().filter((i) => !fitOf(i.job) || fitOf(i.job).model !== model())).slice(0, count);
  if (!items.length) { batch = { items: [], message: "Claude has already analyzed every job on this tab." }; render(); return; }
  const candidate = candidateBlock(doc).length;
  const jobs = items.reduce((sum, i) => sum + jobBlock(i.job).length, 0) / items.length;
  batch = { items, confirm: true, estimate: estimateCost(items.length, candidate, jobs, model()), done: 0 };
  render();
}

async function runBatch() {
  const key = savedKey();
  if (!batch || !key) return;
  const controller = new AbortController();
  Object.assign(batch, { confirm: false, running: true, controller, spent: 0, failed: 0 });
  render();
  let stop = null;
  const one = async (item) => {
    if (stop || controller.signal.aborted) return;
    try {
      const [fit, usage] = await analyzeFit(key, model(), doc, item.job, controller.signal);
      saveFit(item.job, fit);
      recordUsage(usage, model());
      batch.spent += cost(usage, model());
    } catch (err) {
      if (err.name === "AbortError") return;
      batch.failed += 1;
      jobErrors.set(item.job.id, err.message);
      if ([401, 403, 429].includes(err.status) || /credit/i.test(err.message)) stop = err.message;  // the rest would fail too
    }
    batch.done += 1;
    render();
  };
  const [first, ...rest] = batch.items;
  await one(first);  // alone, so the others can reuse its cached prompt
  const queue = [...rest];
  await Promise.all([1, 2, 3].map(async () => { while (queue.length) await one(queue.shift()); }));
  const stopped = controller.signal.aborted ? "Stopped. " : stop ? `Stopped: ${stop} ` : "";
  batch = { items: [], message: `${stopped}Claude analyzed ${plural(batch.done - batch.failed, "job")} (${money(batch.spent)}).${batch.failed && !stop ? ` ${batch.failed} failed; open them to see why.` : ""}` };
  render();
}

// --- settings ---------------------------------------------------------------------------------------
const WORKPLACE_CHOICES = [["remote", "Remote"], ["hybrid", "Hybrid"], ["onsite", "On-site"]];
const LOOKING_FOR = [
  { path: "profile.target_titles", type: "lines", rows: 4, label: "Target job titles", hint: "One per line. Roles you could realistically interview for: your level, or one step up. Only jobs whose title matches one of these show up." },
  { path: "profile.years_experience", type: "number", min: 0, max: 60, step: "0.5", label: "Years of experience" },
  { path: "profile.skills", type: "lines", rows: 4, label: "Skills", hint: "One per line. Jobs that mention them score higher." },
  { path: "settings.filters.title_include", type: "lines", rows: 2, label: "Also show titles containing", hint: "Optional, one per line: more titles to include besides your targets." },
  { path: "profile.current_title", type: "text", label: "Current or most recent title", hint: "Optional. Claude uses it when it reads postings for you." },
];
const WHERE = [
  { path: "settings.filters.locations", type: "lines", rows: 3, label: "Acceptable locations", hint: "One per line, like Austin or Dallas, TX. Leave empty for anywhere." },
  { path: "settings.filters.include_remote", type: "check", label: "Include remote jobs, wherever they're based" },
  { path: "settings.filters.location_exclude", type: "lines", rows: 2, label: "Hide locations containing", hint: "One per line." },
  { path: "profile.workplace_preferences", type: "choices", options: WORKPLACE_CHOICES, label: "Workplaces you'd consider", hint: "These score a little higher; nothing is hidden." },
];
const COMPANIES = [
  { path: "settings.filters.company_include", type: "lines", rows: 3, label: "Only show these companies", hint: "Optional, one per line, as the board writes them. Leave empty to see every company." },
  { path: "settings.filters.company_exclude", type: "lines", rows: 2, label: "Hide these companies", hint: "One per line, like your current employer." },
];
const FILTERS = [
  { path: "settings.filters.title_exclude", type: "lines", rows: 3, label: "Hide titles containing", hint: "One per line, like senior, staff or manager." },
  { path: "settings.filters.max_years_gap", type: "int", min: 0, max: 20, label: "Max years above your experience", hint: "Hides jobs asking for more than your years plus this. Leave empty to turn it off." },
  { path: "settings.filters.max_age_days", type: "int", min: 1, max: 365, label: "Hide postings older than (days)", hint: "Leave empty to turn it off." },
  { path: "settings.filters.min_salary", type: "int", money: true, min: 0, label: "Minimum salary (per year)", hint: "Only applied when a job lists its pay." },
  { path: "settings.filters.description_exclude", type: "lines", rows: 2, label: "Hide jobs whose description mentions", hint: "One per line." },
];
const ELIGIBILITY = [
  { path: "profile.work_authorization", type: "text", label: "Work authorization", placeholder: "For example: US citizen" },
  { path: "profile.requires_sponsorship", type: "check", label: "I need visa sponsorship" },
  { path: "settings.filters.hide_no_sponsorship", type: "check", label: "Hide jobs that say they can't sponsor visas (when you need sponsorship)" },
  { path: "profile.security_clearance", type: "text", label: "Security clearance", placeholder: "Leave empty if you don't have one" },
  { path: "settings.filters.hide_clearance_required", type: "check", label: "Hide jobs that require a security clearance (unless you have one)" },
];
const RESUME = [
  { path: "resume_text", type: "textarea", rows: 8, label: "Resume text", hint: "Optional. Paste the text of your resume. Only Claude uses it, when you ask it to read postings or fill in your settings." },
];

const getPath = (obj, path) => path.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
function setPath(obj, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  let o = obj;
  for (const k of keys) {
    if (!isObject(o[k])) o[k] = {};
    o = o[k];
  }
  o[last] = value;
}

let editor = null;  // the open form; kept while you look at jobs, so edits aren't lost
let msgEl = null, keyBox = null, uid = 0;

function say(text, kind, ...more) {
  msgEl.className = `msg ${kind || ""}`;
  msgEl.replaceChildren(text, ...more);
}

function fieldInput(spec, value) {
  const id = `setting-${++uid}`;
  const hint = spec.hint ? el("span", { class: "hint" }, spec.hint) : null;
  if (spec.type === "check") {
    const input = el("input", { type: "checkbox", id, checked: Boolean(value), "data-path": spec.path });
    return [el("label", { class: "check field" }, input, spec.label), input];
  }
  if (spec.type === "choices") {
    const chosen = Array.isArray(value) ? value : [];
    const input = el("div", { class: "choices", role: "group", "aria-labelledby": id, "data-path": spec.path },
      spec.options.map(([v, label]) => el("label", { class: "check" }, el("input", { type: "checkbox", value: v, checked: chosen.includes(v) }), label)));
    return [el("div", { class: "field" }, el("span", { class: "label", id }, spec.label), input, hint), input];
  }
  let input;
  if (spec.type === "lines" || spec.type === "textarea") {
    const text = spec.type === "lines" && Array.isArray(value) ? value.join("\n") : String(value ?? "");
    const rows = Math.min(12, Math.max(spec.rows || 3, text.split("\n").length + 1));  // show the whole list
    input = el("textarea", { id, rows, placeholder: spec.placeholder, spellcheck: spec.type === "lines" ? "false" : null, "data-path": spec.path }, text);
  } else {
    const numeric = spec.type === "int" || spec.type === "number";
    input = el("input", {
      id, type: numeric ? (spec.money ? "text" : "number") : spec.type, value: value ?? "", placeholder: spec.placeholder,
      min: numeric && !spec.money ? spec.min : null, max: numeric && !spec.money ? spec.max : null, step: spec.step,
      inputmode: spec.money ? "numeric" : null, "data-path": spec.path,
    });
  }
  return [el("div", { class: "field" }, el("label", { for: id }, spec.label), input, hint), input];
}

function readField(spec, input) {
  if (spec.type === "check") return input.checked;
  if (spec.type === "choices") return [...input.querySelectorAll("input:checked")].map((i) => i.value);
  if (spec.type === "lines") return input.value.split("\n").map((s) => s.trim()).filter(Boolean);
  const raw = input.value.trim();
  if (spec.type === "int" || spec.type === "number") {
    if (!raw) return null;
    const thousands = spec.money && /k$/i.test(raw);
    const n = Number(spec.money ? raw.replace(/[$,\s]|k$/gi, "") : raw) * (thousands ? 1000 : 1);
    const range = spec.max !== undefined ? ` from ${spec.min} to ${spec.max}` : "";
    if (!Number.isFinite(n) || n < (spec.min ?? 0) || (spec.max !== undefined && n > spec.max)) throw new Error(`${spec.label}: enter a number${range}.`);
    return spec.type === "int" ? Math.round(n) : n;
  }
  return spec.type === "textarea" ? input.value.trim() : raw;
}

function collect() {
  const out = clone(editor.base);
  for (const [spec, input] of editor.fields) setPath(out, spec.path, readField(spec, input));
  const choice = editor.model.value;
  setPath(out, "settings.ai.model", MODELS.some((m) => m.id === choice) ? choice : DEFAULT_MODEL);
  return normalize(out);
}

function buildSettings(source, message) {
  const valueOf = (path) => { const v = getPath(source, path); return v === undefined ? getPath(DEFAULT_DOC, path) : v; };
  const fields = [];
  const simple = (specs) => specs.map((spec) => {
    const [node, input] = fieldInput(spec, valueOf(spec.path));
    fields.push([spec, input]);
    return node;
  });
  const section = (title, hint, ...body) => el("fieldset", {}, el("legend", {}, title), hint ? el("p", { class: "hint" }, hint) : null, ...body);
  const modelSelect = el("select", { id: "claude-model", "aria-label": "Claude model" },
    MODELS.map((m) => el("option", { value: m.id, selected: m.id === valueOf("settings.ai.model") }, `${m.label}: ${m.note}`)));
  editor = { base: clone(source), fields, model: modelSelect };
  msgEl = el("div", { class: "msg", id: "settings-message", role: "status", "aria-live": "polite" });
  keyBox = el("div");
  const companies = [...new Set(feed.jobs.map((j) => j.company))].sort((a, b) => a.localeCompare(b));
  $("settings-view").replaceChildren(...[
    el("div", { class: "settings-head" }, el("h1", {}, "Settings"), el("button", { type: "button", class: "btn-sm", id: "settings-back", onclick: showJobs }, firstVisit && !local.get("settings", null) ? "Skip for now" : "Back to jobs")),
    firstVisit && !local.get("settings", null)
      ? el("div", { class: "notice info", id: "welcome" }, el("strong", {}, "Welcome! "),
          `This board lists ${plural(feed.jobs.length, "current job")} from a shared feed. Tell it what you're looking for and it ranks them for you. Everything you enter stays in this browser.`)
      : null,
    el("p", { class: "muted" }, "Saved in this browser only. Nothing here is sent anywhere, except to Anthropic when you use Claude with your own key."),
    section("What you're looking for", null, simple(LOOKING_FOR)),
    section("Where", null, simple(WHERE)),
    section("Companies", null, simple(COMPANIES),
      el("details", { class: "hint mt" }, el("summary", {}, `The feed has jobs from ${plural(companies.length, "company", "companies")}`), el("p", {}, companies.join(", ")))),
    section("Filters", "Jobs a filter hides are listed under Hidden by filters, with the reason.", simple(FILTERS)),
    section("Eligibility", null, simple(ELIGIBILITY)),
    section("Resume", null, simple(RESUME), el("div", { class: "btn-row" },
      el("button", { type: "button", id: "fill-from-resume", onclick: fillFromResume }, "Fill in my settings from this resume"),
      el("span", { class: "hint" }, "Uses Claude with your key (less than a cent with Haiku)."))),
    section("Claude (optional)", null, claudeHelp(), keyBox,
      el("div", { class: "field" }, el("label", { for: "claude-model" }, "Model"), modelSelect)),
    section("Back up or import", "Copy your settings to keep a backup or move them to another browser, or paste settings exported from the jobbot app. Your API key is never included.", jsonTools()),
    section("Your data on this device", null, eraseTools()),
    el("div", { class: "save-bar" }, el("button", { type: "button", class: "btn-primary", id: "save-settings", onclick: save }, "Save"), msgEl),
  ].filter(Boolean));
  renderKey();
  if (message) say(message, "good");
}

function claudeHelp() {
  return [
    el("p", { class: "hint" }, "With your own Anthropic API key, Claude can read postings against your resume and rate your chances of getting an interview, and fill in these settings from your resume. Requests go straight from this browser to Anthropic and are billed to your account; this site never sees your key."),
    el("ol", { class: "hint" },
      el("li", {}, external(KEYS_URL, "Create an API key ↗"), " in the Anthropic Console. A separate key just for this site is safest."),
      el("li", {}, "Set a monthly spend limit for it under ", external(LIMITS_URL, "Limits ↗"), "."),
      el("li", {}, "Paste the key below and press Save.")),
  ];
}

function renderKey() {
  const key = savedKey();
  const usage = local.get("usage", { calls: 0, dollars: 0 });
  const input = el("input", { type: "password", id: "anthropic-key", autocomplete: "off", spellcheck: "false", placeholder: key ? "Paste a new key to replace it" : "sk-ant-…", "aria-label": "Anthropic API key" });
  const remember = el("input", { type: "checkbox", id: "remember-key", checked: key ? keyRemembered() : false });
  const status = el("p", { class: "hint", id: "key-status" },
    key ? `A key ending in …${key.slice(-4)} is saved ${keyRemembered() ? "on this device" : "for this tab only"}. ` : "No key saved. Claude features are off.",
    key ? el("button", { type: "button", class: "link", id: "remove-key", onclick: () => { storeKey(null); renderKey(); render(); say("Removed your API key from this browser.", "good"); } }, "Remove it") : null);
  editor.key = input;
  editor.remember = remember;
  keyBox.replaceChildren(
    el("div", { class: "field" }, el("label", { for: "anthropic-key" }, "Anthropic API key"),
      el("div", { class: "key-row" }, input, el("button", { type: "button", id: "test-key", onclick: checkKey }, "Test"))),
    status,
    el("label", { class: "check field" }, remember, "Remember the key on this device. Leave this off on shared computers: then it's forgotten when you close the tab."),
    usage.calls ? el("p", { class: "hint" }, `This browser has made ${plural(usage.calls, "Claude request")}, ${money(usage.dollars)} so far (an estimate; your Anthropic Console has the real numbers).`) : null,
    el("ul", { class: "safety" },
      el("li", {}, "Your key is only ever sent to api.anthropic.com, directly from this browser."),
      el("li", {}, "It isn't in backups, and this page runs no third-party scripts that could read it."),
      el("li", {}, "If you think it leaked, delete it in the Anthropic Console; that stops it working everywhere.")));
}

async function checkKey() {
  const key = editor.key.value.trim() || savedKey();
  if (!key) { say("Paste a key first.", "bad"); return; }
  say("Checking the key…");
  try {
    await testKey(key, editor.model.value);
    say("The key works.", "good");
  } catch (err) {
    say(err.message || String(err), "bad");
  }
}

async function fillFromResume() {
  const key = editor.key.value.trim() || savedKey();
  const resume = editor.fields.find(([spec]) => spec.path === "resume_text")[1].value.trim();
  if (!resume) { say("Paste your resume text first.", "bad"); return; }
  if (!key) { say("This uses Claude: add your Anthropic API key under Claude first.", "bad"); return; }
  const button = $("fill-from-resume");
  button.disabled = true;
  say("Claude is reading your resume…");
  try {
    const [found, usage] = await suggestFromResume(key, editor.model.value, resume);
    recordUsage(usage, editor.model.value);
    const input = (path) => editor.fields.find(([spec]) => spec.path === path)[1];
    const filled = [];
    const setIfEmpty = (path, value, label) => {
      const field = input(path);
      if (value === null || value === "" || (Array.isArray(value) && !value.length) || field.value.trim()) return;
      field.value = Array.isArray(value) ? value.join("\n") : String(value);
      if (field.tagName === "TEXTAREA") field.rows = Math.min(12, Math.max(field.rows, field.value.split("\n").length + 1));
      filled.push(label);
    };
    setIfEmpty("profile.target_titles", found.target_titles, "target titles");
    setIfEmpty("profile.years_experience", found.years_experience, "years of experience");
    setIfEmpty("profile.current_title", found.current_title, "current title");
    const skills = input("profile.skills");
    const have = new Set(skills.value.split("\n").map((s) => s.trim().toLowerCase()).filter(Boolean));
    const added = found.skills.filter((s) => !have.has(s.toLowerCase()));
    if (added.length) {
      skills.value = [...skills.value.split("\n").map((s) => s.trim()).filter(Boolean), ...added].join("\n");
      skills.rows = Math.min(12, Math.max(skills.rows, skills.value.split("\n").length + 1));
      filled.push(plural(added.length, "skill"));
    }
    if (found.summary && !editor.base.profile.summary) editor.base.profile.summary = found.summary;
    say(filled.length ? `Filled in ${filled.join(", ")} from your resume. Check them, then press Save.` : "Your settings already cover what Claude found; nothing was changed.", "good");
  } catch (err) {
    say(err.message || String(err), "bad");
  } finally {
    button.disabled = false;
  }
}

async function parseConfig(text) {
  text = text.trim();
  if (!text) throw new Error("paste your settings first");
  let raw = text;
  if (text.startsWith("jobbot1:")) {  // the compact form the jobbot app copies
    const bytes = Uint8Array.from(atob(text.slice(8).replace(/\s+/g, "")), (c) => c.charCodeAt(0));
    raw = await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).text();
  }
  const parsed = JSON.parse(raw);
  if (!isObject(parsed)) throw new Error("settings must be a JSON object");
  const skipped = ["companies", "searches"].filter((k) => Array.isArray(parsed[k]) && parsed[k].length);
  for (const key of Object.keys(parsed)) if (key.startsWith("_") || key === "companies" || key === "searches") delete parsed[key];
  return [normalize(parsed), skipped];
}

function jsonTools() {
  const box = el("textarea", { rows: 6, class: "mono", id: "settings-json", spellcheck: "false", "aria-label": "Settings as JSON",
    placeholder: "Paste settings as JSON, or the jobbot1:… text from the jobbot app, then press Load into the form." });
  return [box, el("div", { class: "btn-row" },
    el("button", { type: "button", onclick: () => {
      try { box.value = JSON.stringify(collect(), null, 2); box.select(); } catch (err) { say(err.message, "bad"); }
    } }, "Show these settings as JSON"),
    el("button", { type: "button", onclick: async () => {
      if (!box.value) return;
      try { await navigator.clipboard.writeText(box.value); say("Copied.", "good"); } catch (err) { say("Couldn't copy; select the text and copy it yourself.", "bad"); }
    } }, "Copy"),
    el("button", { type: "button", onclick: async () => {
      try {
        const [loaded, skipped] = await parseConfig(box.value);
        const note = skipped.length ? " Companies and searches aren't imported: the board's jobs come from the shared feed." : "";
        buildSettings(loaded, `Loaded. Check the settings, then press Save.${note}`);
      } catch (err) {
        say(`Couldn't load that: ${err.message || err}`, "bad");
      }
    } }, "Load into the form"))];
}

function eraseTools() {
  const button = el("button", { type: "button", id: "erase", class: "btn-ghost" }, "Erase everything on this device");
  button.addEventListener("click", () => {
    if (!button.dataset.armed) {
      button.dataset.armed = "1";
      button.textContent = "Click again to erase your settings, saved jobs, analyses and API key";
      return;
    }
    local.clear();
    session.clear();
    location.reload();
  });
  return [el("p", { class: "hint" }, "Your settings, saved and applied jobs, Claude's analyses and your API key live in this browser's storage. Clearing your browsing data for this site also removes them."), button];
}

function save() {
  let next;
  try {
    next = collect();
  } catch (err) {
    say(err.message, "bad");
    return;
  }
  const typedKey = editor.key.value.trim();
  if (typedKey && !looksLikeKey(typedKey)) {
    say("That doesn't look like an Anthropic API key: they start with sk-ant-. Nothing was saved.", "bad");
    return;
  }
  if (!local.set("settings", next)) {
    say("This browser won't let the page save anything (private browsing or blocked storage?).", "bad");
    return;
  }
  const key = typedKey || savedKey();
  if (key) storeKey(key, editor.remember.checked);
  doc = next;
  editor.base = clone(next);
  $("settings-back").textContent = "Back to jobs";
  if ($("welcome")) $("welcome").remove();
  compute();
  renderKey();
  shown = PAGE;
  render();
  say(`Saved. ${plural(computed.visible.length, "job matches", "jobs match")} your settings. `, "good",
    el("button", { type: "button", class: "link", onclick: showJobs }, "See them"));
}

main();

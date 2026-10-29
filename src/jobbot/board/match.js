// Filters and the estimated fit score, run in the visitor's browser so their settings never leave
// it. A port of jobbot/text.py and jobbot/matching.py: keep the two in step (tests/test_board_js.py
// checks they agree).

const PHRASES = [
  [/\bfront[\s-]?end\b/g, "frontend"],
  [/\bback[\s-]?end\b/g, "backend"],
  [/\bfull[\s-]?stack\b/g, "fullstack"],
  [/\bmachine learning\b/g, "ml"],
  [/\bartificial intelligence\b/g, "ai"],
  [/\bsite reliability\b/g, "sre"],
  [/\bquality assurance\b/g, "qa"],
  [/\buser experience\b/g, "ux"],
  [/\buser interface\b/g, "ui"],
  [/\bdev[\s-]?ops\b/g, "devops"],
  [/\bdata base\b/g, "database"],
  [/\bsr\.?(?=\s)/g, "senior"],
  [/\bjr\.?(?=\s)/g, "junior"],
];
const WORKPLACE_WORDS = new Set(["remote", "hybrid", "onsite"]);
const COMPANY_SUFFIXES = new Set([
  "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
  "plc", "gmbh", "ag", "sa", "bv", "nv", "pbc", "lp", "llp", "the",
]);
const ANNUAL = { year: 1, month: 12, week: 52, day: 260, hour: 2080 };
const LEVEL_MIN_YEARS = { intern: 0, entry: 0, mid: 2, senior: 5, lead: 6, staff: 8, principal: 10, director: 10 };
export const WORKPLACES = ["remote", "hybrid", "onsite"];
const DAY = 86400 * 1000;

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const capitalize = (word) => word.charAt(0).toUpperCase() + word.slice(1);
// Python's round(): halves go to the even number.
function pyRound(x) {
  const r = Math.round(x);
  return Math.abs(x % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r;
}
const years = (value) => String(Number(value));

function stem(word) {
  if (word.length > 6 && word.endsWith("ing")) return word.slice(0, -3);
  if (word.length > 3 && word.endsWith("s") && !word.endsWith("ss")) return word.slice(0, -1);
  return word;
}

export function titleTokens(text) {
  let t = (text || "").toLowerCase();
  for (const [pattern, repl] of PHRASES) t = t.replace(pattern, repl);
  return (t.match(/[a-z0-9+#]+/g) || []).map(stem);
}

// True when every word of `term` appears in `title` (order-insensitive, light stemming).
export function termMatches(term, title) {
  const termWords = titleTokens(term).filter((w) => !WORKPLACE_WORDS.has(w));
  if (!termWords.length) return false;
  const titleWords = new Set(titleTokens(title));
  return termWords.every((w) => titleWords.has(w));
}

// Case-insensitive whole-word search that copes with symbols like C++, C#, .NET, Node.js.
export function keywordInText(keyword, text) {
  const kw = (keyword || "").trim().toLowerCase();
  if (!kw) return false;
  return new RegExp(`(?<![a-z0-9])${escapeRegExp(kw)}(?![a-z0-9+#])`).test((text || "").toLowerCase());
}

export function normalizeTitle(title) {
  let t = (title || "").toLowerCase();
  t = t.replace(/\bsr\.?(?=\s)/g, "senior").replace(/\bjr\.?(?=\s)/g, "junior");
  t = t.replaceAll("&", " and ");
  t = t.replace(/\((?:[^)]*\b(?:remote|hybrid|onsite|on-site|contract|full[- ]time)\b[^)]*)\)/g, " ");
  t = t.replace(/[^a-z0-9+#]+/g, " ");
  return t.split(" ").filter((w) => w && !WORKPLACE_WORDS.has(w)).join(" ");
}

export function normalizeCompany(name) {
  const words = (name || "").toLowerCase().match(/[a-z0-9&]+/g) || [];
  while (words.length && COMPANY_SUFFIXES.has(words[words.length - 1])) words.pop();
  while (words.length && words[0] === "the") words.shift();
  return words.join(" ");
}

export function annualize(amount, period) {
  if (amount === null || amount === undefined) return null;
  return amount * (ANNUAL[period || "year"] ?? 1);
}

export function ageDays(iso, now = Date.now()) {
  if (!iso) return null;
  const at = Date.parse(iso);
  return Number.isNaN(at) ? null : Math.max(0, (now - at) / DAY);
}

const has = (value) => value !== null && value !== undefined;

// The title terms that make a job relevant: your target titles plus "also keep titles".
export function relevanceTerms(doc) {
  const seen = new Set();
  const terms = [];
  for (const term of [...(doc.settings.filters.title_include || []), ...(doc.profile.target_titles || [])]) {
    const key = titleTokens(term).join(" ");
    if (key && !seen.has(key)) {
      seen.add(key);
      terms.push(term.trim());
    }
  }
  return terms;
}

export const isRelevant = (title, terms) => !terms.length || terms.some((term) => termMatches(term, title));

// Why this job fails one of your filters, or null if it passes them all.
export function hiddenReason(job, doc, now = Date.now()) {
  const { profile } = doc;
  const filters = doc.settings.filters;
  const title = job.title || "";
  const location = job.location || "";
  const description = job.description || "";

  for (const keyword of filters.title_exclude || []) {
    if (keyword.trim() && (termMatches(keyword, title) || keywordInText(keyword, title))) {
      return `Title contains “${keyword.trim()}”`;
    }
  }
  const company = normalizeCompany(job.company);
  for (const excluded of filters.company_exclude || []) {
    if (excluded.trim() && normalizeCompany(excluded) === company) return `Company is on your exclude list (${job.company})`;
  }
  const only = (filters.company_include || []).filter((c) => c.trim()).map(normalizeCompany);
  if (only.length && !only.includes(company)) return `Not one of the companies you chose (${job.company})`;
  for (const keyword of filters.description_exclude || []) {
    if (keyword.trim() && keywordInText(keyword, description)) return `Description mentions “${keyword.trim()}”`;
  }
  for (const excluded of filters.location_exclude || []) {
    if (excluded.trim() && keywordInText(excluded, location)) return `Location excluded (${location})`;
  }
  if (job.workplace === "remote" && !filters.include_remote) return "Remote roles are turned off in your filters";
  if ((filters.locations || []).length && location) {
    const matches = filters.locations.some((p) => p.trim() && keywordInText(p, location));
    if (!matches && !(job.workplace === "remote" && filters.include_remote)) return `Location not in your list (${location})`;
  }
  if (has(filters.max_years_gap) && job.years && has(profile.years_experience) && job.years > profile.years_experience + filters.max_years_gap) {
    return `Asks for ${job.years}+ years (you have ${years(profile.years_experience)})`;
  }
  if (filters.min_salary && (job.salary_max || job.salary_min)) {
    const sameCurrency = !job.salary_currency || job.salary_currency === (profile.salary_currency || "USD");
    const top = annualize(job.salary_max || job.salary_min, job.salary_period);
    if (sameCurrency && top !== null && top < filters.min_salary) return `Pay (${job.salary}) is below your minimum`;
  }
  if (profile.requires_sponsorship && filters.hide_no_sponsorship && job.no_sponsorship) return "Says it can't sponsor work visas";
  if (!profile.security_clearance && filters.hide_clearance_required && job.clearance) return "Requires an active security clearance";
  if (filters.max_age_days && job.posted) {
    const age = ageDays(job.posted, now);
    if (age !== null && age > filters.max_age_days) return `Posted ${Math.floor(age)} days ago`;
  }
  return null;
}

function titleOverlap(term, title) {
  const words = new Set(titleTokens(term));
  if (!words.size) return 0;
  const titleWords = new Set(titleTokens(title));
  let common = 0;
  for (const w of words) if (titleWords.has(w)) common += 1;
  return common / words.size;
}

// A transparent 0-100 fit estimate from title, skills, experience, freshness and workplace.
export function estimate(job, doc, terms, now = Date.now()) {
  const { profile } = doc;
  const notes = [];
  const targets = [...(profile.target_titles || []), ...terms];

  // Title (0-30)
  const matched = targets.find((t) => termMatches(t, job.title));
  let titlePts;
  if (matched !== undefined) titlePts = normalizeTitle(matched) === normalizeTitle(job.title) ? 30 : 25;
  else if (targets.length) titlePts = pyRound(20 * Math.max(...targets.map((t) => titleOverlap(t, job.title))));
  else titlePts = 15;

  // Skills (0-35), saturating so a handful of strong matches already scores well
  const haystack = `${job.title}\n${job.description || ""}`;
  const skills = profile.skills || [];
  const matchedSkills = skills.filter((s) => keywordInText(s, haystack));
  let skillsPts;
  if (skills.length) skillsPts = pyRound(35 * (1 - Math.exp(-matchedSkills.length / 4)));
  else {
    skillsPts = 15;
    notes.push("Add skills to your profile for better matching");
  }

  // Experience (0-20)
  const have = profile.years_experience;
  const required = job.years;
  let expPts;
  if (!has(required) || !has(have)) expPts = 14;
  else {
    const gap = required - have;
    expPts = gap <= 0 ? 20 : gap <= 1 ? 14 : gap <= 2 ? 8 : 0;
    if (gap > 0) notes.push(`Asks for ${required}+ years`);
  }
  if (has(have) && job.seniority) {
    if (have + 2 < (LEVEL_MIN_YEARS[job.seniority] ?? 0)) {
      expPts = Math.max(0, expPts - 10);
      notes.push(`${capitalize(job.seniority)}-level title`);
    } else if ((job.seniority === "intern" || job.seniority === "entry") && have >= 5) {
      expPts = Math.max(0, expPts - 8);
      notes.push("Probably too junior for you");
    }
  }

  // Freshness (0-10): applying in the first few days matters
  const age = ageDays(job.posted, now);
  const freshPts = age === null ? 5 : age <= 3 ? 10 : age <= 7 ? 8 : age <= 14 ? 5 : age <= 30 ? 2 : 0;

  // Workplace preference (0-5)
  const prefs = (profile.workplace_preferences || []).length ? profile.workplace_preferences : WORKPLACES;
  let placePts = 5;
  if (job.workplace && !prefs.includes(job.workplace)) {
    placePts = 0;
    notes.push(`${capitalize(job.workplace)} (not your preference)`);
  }

  const total = titlePts + skillsPts + expPts + freshPts + placePts;
  return {
    score: Math.max(0, Math.min(100, total)),
    breakdown: { title: titlePts, skills: skillsPts, experience: expPts, freshness: freshPts, workplace: placePts },
    skills: matchedSkills.slice(0, 25),
    notes,
  };
}

export const band = (score) => (score === null || score === undefined ? "" : score >= 80 ? "high" : score >= 60 ? "medium" : score >= 40 ? "low" : "poor");

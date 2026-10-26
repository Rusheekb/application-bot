"""Text helpers: HTML cleanup, normalization for de-duplication, and information extraction
(years of experience, seniority, workplace type, salary) from job postings."""

from __future__ import annotations

import hashlib
import html as htmllib
import re
from datetime import UTC, datetime, timedelta

from bs4 import BeautifulSoup

_BLOCK_TAGS = [
    "p", "div", "section", "article", "header", "footer", "blockquote", "table", "tr",
    "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "pre",
]  # fmt: skip


def html_to_text(content: str | None) -> str:
    """Convert posting HTML to readable plain text with bullets and paragraph breaks."""
    if not content:
        return ""
    s = content
    # Some APIs (Greenhouse) return HTML that is itself entity-escaped.
    if "&lt;" in s and s.count("<") < s.count("&lt;"):
        s = htmllib.unescape(s)
    if "<" not in s:
        return _tidy(htmllib.unescape(s))
    soup = BeautifulSoup(s, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        # <li><p>text</p></li> is common; keep the bullet on the same line as its text.
        for inner in li.find_all(["p", "div", "span"]):
            inner.unwrap()
        li.insert_before("\n• ")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    return _tidy(soup.get_text())


def _tidy(text: str) -> str:
    lines = [re.sub(r"[ \t ​]+", " ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln == "•":
            continue
        if not ln:
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(ln)
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# Normalization & fingerprints
# ---------------------------------------------------------------------------

_COMPANY_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "plc", "gmbh", "ag", "sa", "bv", "nv", "pbc", "lp", "llp", "the",
}  # fmt: skip
_LOCATION_ALIASES = {
    "nyc": "new york",
    "new york city": "new york",
    "sf": "san francisco",
    "san francisco bay area": "san francisco",
    "bay area": "san francisco",
    "washington dc": "washington",
    "washington d c": "washington",
}
_WORKPLACE_WORDS = {"remote", "hybrid", "onsite", "on site", "in office"}


def normalize_company(name: str) -> str:
    words = re.findall(r"[a-z0-9&]+", (name or "").lower())
    while words and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def normalize_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"\bsr\.?(?=\s)", "senior", t)
    t = re.sub(r"\bjr\.?(?=\s)", "junior", t)
    t = t.replace("&", " and ")
    t = re.sub(r"\((?:[^)]*\b(?:remote|hybrid|onsite|on-site|contract|full[- ]time)\b[^)]*)\)", " ", t)
    t = re.sub(r"[^a-z0-9+#]+", " ", t)
    words = [w for w in t.split() if w not in {"remote", "hybrid", "onsite"}]
    return " ".join(words)


def normalize_location(location: str | None) -> str:
    loc = (location or "").lower().strip()
    if not loc:
        return ""
    if "remote" in loc or "anywhere" in loc:
        return "remote"
    first = re.split(r"[,;|/(]| - | or ", loc)[0]
    first = re.sub(r"[^a-z0-9 ]+", " ", first)
    first = re.sub(r"\s+", " ", first).strip()
    return _LOCATION_ALIASES.get(first, first)


def job_fingerprint(company: str, title: str, location: str | None) -> str:
    key = f"{normalize_company(company)}|{normalize_title(title)}|{normalize_location(location)}"
    return hashlib.sha1(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Tokens for title matching
# ---------------------------------------------------------------------------

_PHRASES = [
    (r"\bfront[\s-]?end\b", "frontend"),
    (r"\bback[\s-]?end\b", "backend"),
    (r"\bfull[\s-]?stack\b", "fullstack"),
    (r"\bmachine learning\b", "ml"),
    (r"\bartificial intelligence\b", "ai"),
    (r"\bsite reliability\b", "sre"),
    (r"\bquality assurance\b", "qa"),
    (r"\buser experience\b", "ux"),
    (r"\buser interface\b", "ui"),
    (r"\bdev[\s-]?ops\b", "devops"),
    (r"\bdata base\b", "database"),
    (r"\bsr\.?(?=\s)", "senior"),
    (r"\bjr\.?(?=\s)", "junior"),
]


def _stem(word: str) -> str:
    if len(word) > 6 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def title_tokens(text: str) -> list[str]:
    t = (text or "").lower()
    for pattern, repl in _PHRASES:
        t = re.sub(pattern, repl, t)
    return [_stem(w) for w in re.findall(r"[a-z0-9+#]+", t)]


def term_matches(term: str, title: str) -> bool:
    """True when every word of `term` appears in `title` (order-insensitive, light stemming)."""
    term_words = [w for w in title_tokens(term) if w not in {"remote", "hybrid", "onsite"}]
    if not term_words:
        return False
    title_words = set(title_tokens(title))
    return all(w in title_words for w in term_words)


def keyword_in_text(keyword: str, text: str) -> bool:
    """Case-insensitive whole-word search that copes with symbols like C++, C#, .NET, Node.js."""
    kw = keyword.strip().lower()
    if not kw:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9+#])"
    return re.search(pattern, text.lower()) is not None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
}  # fmt: skip
_NUM = r"(\d{1,2}|" + "|".join(_NUM_WORDS) + r")"
_YEARS_PATTERNS = [
    # "5+ years", "3-5 years", "5 to 7 yrs", "five (5) years"
    re.compile(
        rf"\b{_NUM}(?:\s*\(\d{{1,2}}\))?\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*{_NUM}\s*\+?\s*)?(?:years?|yrs?)\b",
        re.I,
    ),
    re.compile(
        rf"\b(?:at least|minimum of|minimum|min\.?|over)\s+{_NUM}(?:\s*\(\d{{1,2}}\))?\s*(?:years?|yrs?)\b",
        re.I,
    ),
    re.compile(rf"\b{_NUM}\s+or more\s+(?:years?|yrs?)\b", re.I),
]
_PREFERRED_RE = re.compile(
    r"\b(preferred|nice to have|nice-to-have|bonus|a plus|is a plus|ideally|desired)\b", re.I
)
_NOT_EXPERIENCE_RE = re.compile(r"\b(years? old|years? ago|founded|in business|anniversary|warranty)\b", re.I)


def _to_int(token: str) -> int | None:
    token = token.lower()
    if token.isdigit():
        return int(token)
    return _NUM_WORDS.get(token)


def extract_years_required(text: str | None) -> int | None:
    """Minimum years of experience the posting asks for (ignoring "preferred" lines)."""
    if not text:
        return None
    found: list[int] = []
    for sentence in re.split(r"(?<=[.;!?])\s+|\n+", text):
        if "experience" not in sentence.lower() and "exp." not in sentence.lower():
            continue
        if _PREFERRED_RE.search(sentence) or _NOT_EXPERIENCE_RE.search(sentence):
            continue
        for pattern in _YEARS_PATTERNS:
            for match in pattern.finditer(sentence):
                value = _to_int(match.group(1))
                if value is not None and 1 <= value <= 25:
                    found.append(value)
    return min(found) if found else None


_SENIORITY_RULES = [
    ("director", r"\b(director|vice president|vp|svp|evp|head of|chief|cto|cio|ciso)\b"),
    ("principal", r"\b(principal|distinguished|fellow)\b"),
    ("staff", r"\bstaff\b"),
    ("lead", r"\b(lead|tech lead|team lead)\b"),
    ("senior", r"\b(senior|sr\.?)\b|\b(?:engineer|developer|analyst|scientist|designer)\s+(?:iii|3)\b"),
    (
        "mid",
        r"\b(mid[- ]?level|intermediate)\b|\b(?:engineer|developer|analyst|scientist|designer)\s+(?:ii|2)\b",
    ),
    ("intern", r"\b(intern|internship|co-?op)\b"),
    (
        "entry",
        r"\b(junior|jr\.?|entry[- ]level|new grad(?:uate)?|graduate|apprentice|early career)\b"
        r"|\b(?:engineer|developer|analyst|scientist|designer)\s+(?:i|1)\b",
    ),
]
SENIORITY_ORDER = ["intern", "entry", "mid", "senior", "lead", "staff", "principal", "director"]


def detect_seniority(title: str) -> str | None:
    t = (title or "").lower()
    for level, pattern in _SENIORITY_RULES:
        if re.search(pattern, t):
            return level
    return None


def detect_workplace(title: str, location: str | None, description: str | None = None) -> str | None:
    head = f"{title} {location or ''}".lower()
    if "hybrid" in head:
        return "hybrid"
    if re.search(r"\b(remote|work from home|wfh|anywhere|distributed)\b", head):
        return "remote"
    if re.search(r"\b(on[- ]?site|in[- ]office)\b", head):
        return "onsite"
    body = (description or "")[:4000].lower()
    if re.search(r"\bhybrid\b", body):
        return "hybrid"
    if re.search(
        r"\b(fully remote|100% remote|remote[- ]first|this (?:is a|role is) remote|work remotely)\b", body
    ):
        return "remote"
    return None


_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}
_AMOUNT = r"(\d{1,3}(?:[,.]\d{3})+|\d+(?:\.\d+)?)\s*([kK])?"
_CUR = r"(\$|€|£|₹|USD|EUR|GBP|CAD|AUD|INR|CA\$|A\$)"
_SALARY_RE = re.compile(
    rf"{_CUR}\s?{_AMOUNT}(?:\s*(?:/|per)\s*(?:hour|hr|year|yr))?\s*(?:-|–|—|to)\s*{_CUR}?\s?{_AMOUNT}"
    r"(?:\s*(?:USD|EUR|GBP|CAD|AUD))?"
    r"(?:\s*(?:per|/|an?|each)\s*(hour|hr|year|yr|annum|annually|month|mo|week|day))?",
    re.I,
)


def _amount(number: str, k: str | None) -> float:
    digits = number.replace(",", "")
    # "120.000" (European thousands separator) vs "52.50" (decimal)
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", digits):
        digits = digits.replace(".", "")
    value = float(digits)
    return value * 1000 if k else value


def parse_salary(text: str | None) -> tuple[float, float, str, str] | None:
    """Find a salary range like "$120,000 - $150,000" or "$55/hr - $70/hr" in free text.

    Returns (min, max, currency, period) where period is one of year/month/week/day/hour.
    """
    if not text:
        return None
    for m in _SALARY_RE.finditer(text):
        cur, a, ak, cur2, b, bk, per = m.groups()
        low, high = _amount(a, ak), _amount(b, bk or ak)
        if bk and not ak and low < 1000 <= high:
            low = _amount(a, bk)  # "$120-150k" means 120k to 150k
        if high < low or low <= 0:
            continue
        currency = _CURRENCY_SYMBOLS.get(cur, cur.upper().replace("CA$", "CAD").replace("A$", "AUD"))
        per = (per or "").lower()
        if per in ("hour", "hr"):
            period = "hour"
        elif per in ("month", "mo"):
            period = "month"
        elif per == "week":
            period = "week"
        elif per == "day":
            period = "day"
        elif per:
            period = "year"
        elif high < 300:
            period = "hour"
        elif high < 1000:
            continue  # too ambiguous ("$200 - $500 bonus")
        elif high < 20000:
            period = "month"
        else:
            period = "year"
        return low, high, currency, period
    return None


_ANNUAL = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080}


def annualize(amount: float | None, period: str | None) -> float | None:
    if amount is None:
        return None
    return amount * _ANNUAL.get(period or "year", 1)


def format_salary(low: float | None, high: float | None, currency: str | None, period: str | None) -> str:
    if low is None and high is None:
        return ""
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "CAD": "CA$", "AUD": "A$"}.get(
        currency or "USD", f"{currency} "
    )

    def fmt(v: float) -> str:
        if period in (None, "year") and v >= 1000:
            return f"{symbol}{v / 1000:.0f}k"
        return f"{symbol}{v:,.0f}" if v >= 100 else f"{symbol}{v:,.2f}".rstrip("0").rstrip(".")

    parts = [fmt(v) for v in (low, high) if v is not None]
    if len(parts) == 2 and parts[0] == parts[1]:
        parts = parts[:1]
    suffix = {"hour": "/hr", "month": "/mo", "week": "/wk", "day": "/day"}.get(period or "", "")
    return "–".join(parts) + suffix


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def parse_datetime(value: object) -> datetime | None:
    """Parse ISO strings, epoch seconds/milliseconds or dates into naive UTC datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # milliseconds
            ts /= 1000
        try:
            dt = datetime.fromtimestamp(ts, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif hasattr(value, "year") and hasattr(value, "month"):  # datetime.date
        dt = datetime(value.year, value.month, value.day)  # type: ignore[attr-defined]
    else:
        s = str(value).strip()
        if s.isdigit():
            return parse_datetime(int(s))
        s = s.replace("Z", "+00:00").replace(" UTC", "+00:00")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\+00:00)?", s):
            s = s.replace(" ", "T", 1)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def parse_relative_posted(text: str | None, now: datetime | None = None) -> datetime | None:
    """Parse strings like "Posted Today", "Posted 3 Days Ago", "Posted 30+ Days Ago"."""
    if not text:
        return None
    now = now or datetime.now(UTC).replace(tzinfo=None)
    t = text.lower()
    if "today" in t or "just posted" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*(hour|day|week|month)s?", t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    delta = {"hour": timedelta(hours=n), "day": timedelta(days=n), "week": timedelta(weeks=n)}.get(
        unit, timedelta(days=30 * n)
    )
    return now - delta


def age_days(dt: datetime | None, now: datetime | None = None) -> float | None:
    if dt is None:
        return None
    now = now or datetime.now(UTC).replace(tzinfo=None)
    return max(0.0, (now - dt).total_seconds() / 86400)


def time_ago(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return ""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    days = seconds // 86400
    if days < 60:
        return f"{days}d ago"
    return f"{days // 30}mo ago"


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

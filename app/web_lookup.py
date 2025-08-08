
from typing import List, Tuple
import time, math, random, json, sqlite3
from pathlib import Path

import httpx
import re

BASE_DIR = Path(__file__).resolve().parent
CACHE_DB = BASE_DIR / "webcache.sqlite3"

def _db():
    conn = sqlite3.connect(CACHE_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, ts REAL)")
    return conn

def _cache_get(key: str):
    try:
        conn = _db()
        cur = conn.execute("SELECT value, ts FROM cache WHERE key=?", (key,))
        row = cur.fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception:
        pass
    return None

def _cache_set(key: str, value):
    try:
        conn = _db()
        conn.execute("INSERT OR REPLACE INTO cache(key,value,ts) VALUES(?,?,?)", (key, json.dumps(value), time.time()))
        conn.commit()
        conn.close()
    except Exception:
        pass

UA = "AcronymExtractor/1.0 (+https://example.invalid)"
TIMEOUT = 5.0

def _backoff_sleep(attempt: int):
    time.sleep(min(1.5, 0.15 * (2 ** attempt) + random.random() * 0.1))

def _http_get_json(url: str, params: dict) -> dict | list | None:
    headers = {"User-Agent": UA}
    for attempt in range(3):
        try:
            with httpx.Client(timeout=TIMEOUT, headers=headers) as client:
                r = client.get(url, params=params)
                if r.status_code == 429:
                    _backoff_sleep(attempt)
                    continue
                if r.status_code >= 500:
                    _backoff_sleep(attempt)
                    continue
                if r.status_code != 200:
                    return None
                ct = r.headers.get("content-type","")
                if "json" in ct or r.text.strip().startswith("{") or r.text.strip().startswith("["):
                    return r.json()
                return None
        except Exception:
            _backoff_sleep(attempt)
    return None

def _score_candidate(acr: str, text: str, context: str) -> float:
    # Heuristic: initials alignment + context overlap
    a = acr.upper()
    t = (text or "").strip()
    if not t: return 0.0
    # initials
    words = [w for w in re_split_nonword(t) if w]
    initials = "".join(w[0].upper() for w in words if w[0].isalnum())
    align = 1.0 if initials == a else (0.7 if a in initials or initials in a else 0.0)
    # context overlap (bag of lowercase nouns-ish)
    ctx = " ".join(re_split_nonword(context.lower()))[:500]
    bonus = 0.0
    for key in ("computer","data","network","law","regulation","europe","united","states","protocol","web","page","pdf","memory","processor","graphics","artificial","intelligence","health","organization","university","union","nation"):
        if key in ctx and key in t.lower():
            bonus += 0.04
    return max(0.1, min(0.9, 0.5 + 0.3*align + bonus))

def re_split_nonword(s: str):
    import re
    return re.split(r"[^A-Za-z0-9]+", s)

def wikipedia_opensearch(acr: str, keyword: str | None = None):
    q = acr if not keyword else f"{acr} {keyword}"
    url = "https://en.wikipedia.org/w/api.php"
    params = {"action":"opensearch","limit":"6","namespace":"0","format":"json","search":q}
    data = _http_get_json(url, params)
    out = []
    if isinstance(data, list) and len(data) >= 4:
        titles, descs, links = data[1], data[2], data[3]
        for t, d, l in zip(titles, descs, links):
            txt = (d or t or "").strip()
            if txt and not _is_disambiguation_text(txt):
                norm = normalize_definition(acr, txt, title_hint=t)
                if norm:
                    out.append((norm, "en.wikipedia.org", 0.58))
    return out

def wikipedia_rest_summary(acr: str, keyword: str | None = None):
    page = acr if not keyword else f"{acr} ({keyword})"
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{page}"
    data = _http_get_json(url, {})
    out = []
    if isinstance(data, dict):
        txt = (data.get("extract") or data.get("description") or "").strip()
        if txt and not _is_disambiguation_text(txt):
            norm = normalize_definition(acr, txt, title_hint=data.get("title"))
            if norm:
                out.append((norm, "en.wikipedia.org", 0.64))
    return out

def wiktionary_search(acr: str, keyword: str | None = None):
    q = acr if not keyword else f"{acr} {keyword}"
    url = "https://en.wiktionary.org/w/api.php"
    params = {"action":"opensearch","format":"json","limit":"5","search":q}
    data = _http_get_json(url, params)
    out = []
    if isinstance(data, list) and len(data) >= 2:
        titles = data[1]
        for t in titles:
            if t:
                out.append((t, "en.wiktionary.org", 0.45))
    return out

def wikidata_search(acr: str, keyword: str | None = None):
    q = acr if not keyword else f"{acr} {keyword}"
    url = "https://www.wikidata.org/w/api.php"
    params = {"action":"wbsearchentities","format":"json","language":"en","limit":"6","search":q}
    data = _http_get_json(url, params)
    out = []
    if isinstance(data, dict) and isinstance(data.get('search'), list):
        for item in data['search']:
            desc = (item.get('description') or item.get('label') or '').strip()
            if desc:
                out.append((desc, "www.wikidata.org", 0.5))
    return out


def web_candidates(acr: str, context_text: str, limit: int = 5, keyword: str | None = None):
    key = f"{acr}|{(keyword or '').strip().lower()}"
    cached = _cache_get(key)
    if cached:
        return cached[:limit]

    out = []
    seen = set()

    def add(items):
        for defn, dom, sc in (items or []):
            d = (defn or '').strip()
            if not d or _is_disambiguation_text(d): 
                continue
            k = (d.lower(), dom)
            if k in seen: 
                continue
            seen.add(k)
            # re-score with context + initials
            score = max(sc, _score_candidate(acr, d, context_text))
            out.append((d, dom, score))

    # Prefer a title->summary chain first (better than raw opensearch descs)
    add(wikipedia_title_search(acr, keyword))
    if len(out) < limit: add(wikipedia_rest_summary(acr, keyword))
    if len(out) < limit: add(wikipedia_opensearch(acr, keyword))
    if len(out) < limit: add(wiktionary_search(acr, keyword))
    if len(out) < limit: add(wikidata_search(acr, keyword))

    out = _prefer_exact_initials(acr, out)
    out = sorted(out, key=lambda x: x[2], reverse=True)[:limit]
    _cache_set(key, out)
    return out


def _is_disambiguation_text(text: str) -> bool:
    t = (text or '').lower()
    return ('may refer to' in t) or ('disambiguation' in t) or ('list of' in t)

def _initials(s: str) -> str:
    parts = re.split(r'[^A-Za-z0-9]+', s or '')
    return ''.join([p[0].upper() for p in parts if p])

def _prefer_exact_initials(acr: str, defs):
    a = acr.upper()
    # boost exact-match initials, then partial, then others
    scored = []
    for (d, dom, sc) in defs:
        ini = _initials(d)
        bonus = 0.0
        if ini == a: bonus = 0.3
        elif a in ini or ini in a: bonus = 0.15
        scored.append((d, dom, min(0.95, sc + bonus)))
    return scored

def wikipedia_title_search(acr: str, keyword: str | None = None):
    q = acr if not keyword else f"{acr} {keyword}"
    url = "https://en.wikipedia.org/w/api.php"
    data = _http_get_json(url, {"action":"opensearch","limit":"6","namespace":"0","format":"json","search":q})
    titles = []
    if isinstance(data, list) and len(data) >= 2:
        titles = data[1] or []
    out = []
    for t in titles[:3]:
        u = f"https://en.wikipedia.org/api/rest_v1/page/summary/{t}"
        js = _http_get_json(u, {})
        if isinstance(js, dict):
            txt = (js.get("extract") or js.get("description") or "").strip()
            if txt and not _is_disambiguation_text(txt):
                norm = normalize_definition(acr, txt, title_hint=t)
                if norm:
                    out.append((norm, "en.wikipedia.org", 0.68))
    return out


# --- Definition normalization: extract clean expansion only ---
def _split_sentences(text: str):
    # very light split; we want first sentence mostly
    return re.split(r'(?<=[.!?])\s+', text or '')

def _clean_phrase(s: str):
    s = re.sub(r'\s+', ' ', (s or '').strip())
    # remove trailing punctuation
    s = re.sub(r'[;,:.\-]+$', '', s)
    # drop leading articles
    s = re.sub(r'^(the|a|an)\s+', '', s, flags=re.I)
    return s.strip()

def _initials(s: str):
    parts = re.split(r'[^A-Za-z0-9]+', s or '')
    return ''.join([p[0].upper() for p in parts if p])

def _looks_like_all_caps(s: str):
    return bool(re.fullmatch(r'[A-Z0-9]+', (s or '').strip()))

def normalize_definition(acr: str, source_text: str, title_hint: str | None = None):
    A = acr.upper()
    # 1) If title looks like a proper expansion (not all caps) and initials match, take it
    if title_hint and not _looks_like_all_caps(title_hint) and _initials(title_hint) == A:
        cand = _clean_phrase(title_hint)
        if cand.upper() != A and len(cand) > len(A)+1:
            return cand

    t = (source_text or '').strip()
    first = _split_sentences(t)[0] if t else ''
    first = first.strip()

    # 2) Common patterns
    patterns = [
        r'\bstands for\b\s+([^.;:,]+)',
        r'\bis an?\s+([^.;:,]+)',
        r'\bis the\s+([^.;:,]+)',
        r'\bacronym for\b\s+([^.;:,]+)',
        r'\babbreviation for\b\s+([^.;:,]+)',
        r'\bshort for\b\s+([^.;:,]+)',
        r'\bmeaning\b\s+([^.;:,]+)',
    ]
    for pat in patterns:
        m = re.search(pat, first, flags=re.I)
        if m:
            cand = _clean_phrase(m.group(1))
            if _initials(cand) in (A, ) or A in _initials(cand) or len(cand.split()) <= 8:
                if cand.upper() != A and len(cand) > len(A)+1:
                    return cand

    # 3) Fallback: longest capitalized chunk whose initials match
    tokens = re.split(r'[^A-Za-z0-9]+', first)
    best = []
    for i in range(len(tokens)):
        for j in range(i+1, min(i+8, len(tokens))+1):
            phrase = ' '.join(tokens[i:j])
            if len(phrase) < len(A)+1:
                continue
            if _initials(phrase) == A:
                best.append(phrase)
    if best:
        best.sort(key=len, reverse=True)
        cand = _clean_phrase(best[0])
        if cand.upper() != A and len(cand) > len(A)+1:
            return cand

    # 4) If nothing worked, return empty so caller can drop it
    return ''

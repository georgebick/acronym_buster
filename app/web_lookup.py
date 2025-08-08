
from typing import List, Tuple
import time, math, random, json, sqlite3
from pathlib import Path

import httpx

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
            if txt:
                out.append((txt, "en.wikipedia.org", 0.55))
    return out

def wikipedia_rest_summary(acr: str, keyword: str | None = None):
    # Try reading page summary for exact acronym
    page = acr if not keyword else f"{acr} ({keyword})"
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{page}"
    data = _http_get_json(url, {})
    out = []
    if isinstance(data, dict):
        txt = (data.get("extract") or data.get("description") or "").strip()
        if txt:
            out.append((txt.split(".")[0], "en.wikipedia.org", 0.58))
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


def web_candidates(acr: str, context_text: str, limit: int = 5, keyword: str | None = None) -> List[Tuple[str,str,float]]:
    # caching
    key = f"{acr}|{(keyword or '').strip().lower()}"
    cached = _cache_get(key)
    if cached:
        return cached[:limit]

    out = []
    seen = set()
    def add(items):
        for defn, dom, sc in (items or []):
            k = (defn.strip().lower(), dom)
            if k in seen: continue
            seen.add(k)
            score = sc
            score = max(score, _score_candidate(acr, defn, context_text))
            out.append((defn, dom, score))

    add(wikipedia_rest_summary(acr, keyword))
    if len(out) < limit: add(wikipedia_opensearch(acr, keyword))
    if len(out) < limit: add(wiktionary_search(acr, keyword))
    if len(out) < limit: add(wikidata_search(acr, keyword))

    out = sorted(out, key=lambda x: x[2], reverse=True)[:limit]
    _cache_set(key, out)
    return out

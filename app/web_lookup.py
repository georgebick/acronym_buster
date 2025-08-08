
from typing import List, Tuple
import time, math, random, json, sqlite3
from pathlib import Path

import httpx
from functools import lru_cache
CACHE = {}
import urllib.parse as _url
import re
import logging

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
import logging
    return re.split(r"[^A-Za-z0-9]+", s)

def wikipedia_opensearch(acr: str, keyword: str | None = None, lang: str = 'en'):
    q = acr if not keyword else f"{acr} {keyword}"
    url = f"https://{lang}.wikipedia.org/w/api.php"
    params = {"action":"opensearch","limit":"6","namespace":"0","format":"json","search":q}
    data = _http_get_json(url, params)
    out = []
    if isinstance(data, list) and len(data) >= 4:
        titles, descs, links = data[1], data[2], data[3]
        for t, d, l in zip(titles, descs, links):
            txt = (d or t or "").strip()
            if txt and not _is_disambiguation_text(txt):
                norm = normalize_definition2(acr, txt, title_hint=t)
                if norm:
                    out.append((norm, "en.wikipedia.org", 0.58))
    return out

def wikipedia_rest_summary(acr: str, keyword: str | None = None, lang: str = 'en'):
    page = acr if not keyword else f"{acr} ({keyword})"
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{page}"
    data = _http_get_json(url, {})
    out = []
    if isinstance(data, dict):
        txt = (data.get("extract") or data.get("description") or "").strip()
        if txt and not _is_disambiguation_text(txt):
            norm = normalize_definition2(acr, txt, title_hint=data.get("title"))
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


def web_candidates(acr: str, context_text: str, limit: int = 5, keyword: str | None = None, lang: str = 'en', domain: str | None = None, strict_initials: bool = False):
    key = f"{acr}|{(keyword or '').strip().lower()}|{lang}|{domain}|{strict_initials}"
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
    add(wikipedia_title_search(acr, keyword, lang=lang))
    if len(out) < limit: add(wikipedia_rest_summary(acr, keyword, lang=lang))
    if len(out) < limit: add(wikipedia_opensearch(acr, keyword, lang=lang))
    if len(out) < limit: add(wiktionary_search(acr, keyword))
    if len(out) < limit: add(wikidata_search(acr, keyword))
    # DBpedia & IETF last to reduce API load
    if len(out) < limit: add(dbpedia_search(acr, keyword, lang=lang))
    if len(out) < limit: add(ietf_glossary(acr, keyword))

    # ORDER_START
    # Domain-aware ordering
    if (domain or '').lower() in ('bio','medical','medicine','biomed'):
        add(acromine_lookup(acr))
        if len(out) < limit: add(wiktionary_summary(acr, lang=lang))
        if len(out) < limit: add(wikipedia_search_titles(acr, keyword, lang=lang))
    else:
        add(wikipedia_search_titles(acr, keyword, lang=lang))
        if len(out) < limit: add(wiktionary_summary(acr, lang=lang))
        if len(out) < limit: add(acromine_lookup(acr))
    if len(out) < limit: add(wikipedia_title_search(acr, keyword, lang=lang))
    if len(out) < limit: add(wikipedia_rest_summary(acr, keyword, lang=lang))
    if len(out) < limit: add(wikipedia_opensearch(acr, keyword, lang=lang))
    if len(out) < limit: add(wiktionary_search(acr, keyword))
    if len(out) < limit: add(wikidata_search(acr, keyword))
    if len(out) < limit: add(dbpedia_search(acr, keyword, lang=lang))
    if len(out) < limit: add(ietf_glossary(acr, keyword))
    # Optional API-keyed sources (low priority)
    if len(out) < limit: add(freedict_lookup(acr, lang=lang))
    if len(out) < limit: add(wordnik_lookup(acr))
    if len(out) < limit: add(merriam_webster_lookup(acr))
    if len(out) < limit: add(wordsapi_lookup(acr))
    if len(out) < limit: add(definitions_net_lookup(acr))
    # ORDER_END
    out = _prefer_exact_initials(acr, out)
    # strict filter if requested
    if strict_initials:
        out = [x for x in out if _initials(x[0]) == acr.upper()]
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

def wikipedia_title_search(acr: str, keyword: str | None = None, lang: str = 'en'):
    q = acr if not keyword else f"{acr} {keyword}"
    url = f"https://{lang}.wikipedia.org/w/api.php"
    data = _http_get_json(url, {"action":"opensearch","limit":"6","namespace":"0","format":"json","search":q})
    titles = []
    if isinstance(data, list) and len(data) >= 2:
        titles = data[1] or []
    out = []
    for t in titles[:3]:
        u = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{t}"
        js = _http_get_json(u, {})
        if isinstance(js, dict):
            txt = (js.get("extract") or js.get("description") or "").strip()
            if txt and not _is_disambiguation_text(txt):
                norm = normalize_definition2(acr, txt, title_hint=t)
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

def normalize_definition2(acr: str, source_text: str, title_hint: str | None = None):
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


def _accept_expansion(acr: str, phrase: str, strict: bool = False) -> bool:
    A = acr.upper()
    if not phrase: return False
    if phrase.strip().upper() == A: return False
    ini = _initials(phrase)
    if strict:
        return ini == A
    return (ini == A) or (A in ini) or (ini in A)

def dbpedia_search(acr: str, keyword: str | None = None, lang: str = "en"):
    # Use DBpedia spotlight/lookup-lite via SPARQL: fetch rdfs:label matching acronym or label containing it
    # We prefer labels whose initials match.
    sparql = f"""SELECT DISTINCT ?label ?comment WHERE {{
  ?s rdfs:label ?label . FILTER (lang(?label) = '{lang}').
  OPTIONAL {{ ?s rdfs:comment ?comment . FILTER (lang(?comment) = '{lang}') }}
  FILTER (CONTAINS(LCASE(?label), LCASE('{acr}')))
}} LIMIT 10
"""
    url = "https://dbpedia.org/sparql"
    q = {"query": sparql, "format":"application/sparql-results+json"}
    data = _http_get_json(url, q)
    out = []
    if isinstance(data, dict):
        bindings = (data.get('results') or {}).get('bindings') or []
        for b in bindings:
            lab = (b.get('label',{}).get('value') or '').strip()
            com = (b.get('comment',{}).get('value') or '').strip()
            if lab and _accept_expansion(acr, lab):
                out.append((lab, "dbpedia.org", 0.53))
            elif com:
                # mine comment
                norm = normalize_definition2(acr, com, title_hint=lab)
                if norm and _accept_expansion(acr, norm):
                    out.append((norm, "dbpedia.org", 0.5))
    return out

def ietf_glossary(acr: str, keyword: str | None = None):
    # RFC Editor glossary JSON (static URL often used in docs; may change). 
    # We'll try a simple heuristic via Wikipedia first if IETF not reachable.
    # Placeholder: attempt well-known terms
    common = {
        "HTTP": "Hypertext Transfer Protocol",
        "URL": "Uniform Resource Locator",
        "TLS": "Transport Layer Security",
        "TCP": "Transmission Control Protocol",
        "UDP": "User Datagram Protocol",
        "IP": "Internet Protocol",
        "DNS": "Domain Name System",
        "FTP": "File Transfer Protocol",
        "SMTP": "Simple Mail Transfer Protocol",
        "IMAP": "Internet Message Access Protocol",
        "SSH": "Secure Shell",
    }
    out = []
    val = common.get(acr.upper())
    if val:
        out.append((val, "ietf-glossary", 0.62))
    return out


def normalize_definition2(acr: str, source_text: str, title_hint: str | None = None):
    import re
import logging
    A = acr.upper()
    def _initials_local(s: str):
        parts = re.split(r'[^A-Za-z0-9]+', s or '')
        return ''.join([p[0].upper() for p in parts if p])
    def looks_caps(s: str): return bool(re.fullmatch(r'[A-Z0-9]+', (s or '').strip()))
    def clean(s: str):
        s = re.sub(r'\s+', ' ', (s or '').strip())
        s = re.sub(r'^[\-\–\:]+', '', s).strip()
        s = re.sub(r'[;,:.\-]+$', '', s)
        s = re.sub(r'^(the|a|an)\s+', '', s, flags=re.I)
        return s.strip()

    # Title-based
    if title_hint and not looks_caps(title_hint):
        ti = clean(title_hint)
        if _initials_local(ti) == A and ti.upper() != A and len(ti.split()) <= 8:
            return ti

    t = (source_text or '').strip()
    first = re.split(r'(?<=[.!?])\s+', t)[0] if t else ''

    # Parenthetical: Expansion (ACR) and ACR (Expansion)
    m = re.search(r'^([^()]{2,120})\s*\(\s*' + re.escape(A) + r'\s*\)', first)
    if m:
        cand = clean(m.group(1))
        if _initials_local(cand) == A and cand.upper() != A:
            return cand
    m = re.search(r'^' + re.escape(A) + r'\s*\(\s*([^()]{2,120})\s*\)', first)
    if m:
        cand = clean(m.group(1))
        if _initials_local(cand) == A and cand.upper() != A:
            return cand

    # Cue phrases
    cues = [
        r'\bstands for\b\s+([^.;:,()]+)',
        r'\bis an?\b\s+([^.;:,()]+)',
        r'\bis the\b\s+([^.;:,()]+)',
        r'\bacronym for\b\s+([^.;:,()]+)',
        r'\babbreviation for\b\s+([^.;:,()]+)',
        r'\bshort for\b\s+([^.;:,()]+)',
        r'\bmeaning\b\s+([^.;:,()]+)',
    ]
    for pat in cues:
        m = re.search(pat, first, flags=re.I)
        if m:
            cand = clean(m.group(1))
            ini = _initials_local(cand)
            if cand.upper() != A and 2 <= len(cand.split()) <= 8 and (ini == A or A in ini):
                return cand

    # Longest phrase matching initials
    toks = re.split(r'[^A-Za-z0-9]+', first)
    best = []
    for i in range(len(toks)):
        for j in range(i+2, min(i+9, len(toks))+1):
            phrase = ' '.join(toks[i:j]).strip()
            if len(phrase) < len(A)+1: continue
            if _initials_local(phrase) == A:
                best.append(phrase)
    if best:
        best.sort(key=lambda s: (len(s.split()), len(s)), reverse=True)
        cand = clean(best[0])
        if cand.upper() != A:
            return cand
    return ""


def acromine_lookup(acr: str):
    # http://www.nactem.ac.uk/software/acromine/rest.html
    url = "http://www.nactem.ac.uk/software/acromine/dictionary.py"
    try:
        data = _http_get_json(url, {"sf": acr.upper()})
        out = []
        if isinstance(data, list) and data:
            lfs = (data[0] or {}).get("lfs") or []
            for item in lfs[:8]:
                lf = (item.get("lf") or "").strip()
                if lf and _accept_expansion(acr, lf):
                    # Acromine freq may be present; scale confidence a bit
                    freq = float(item.get("freq", 0) or 0)
                    sc = 0.75 if freq > 0 else 0.65
                    out.append((lf, "acromine", sc))
        return out
    except Exception:
        return []



def wiktionary_summary(acr: str, lang: str = "en"):
    # Try page summary first; fall back to opensearch
    page = acr
    url = f"https://{lang}.wiktionary.org/api/rest_v1/page/summary/{_url.quote(page)}"
    js = _http_get_json(url, {})
    out = []
    if isinstance(js, dict):
        txt = (js.get("extract") or js.get("description") or "").strip()
        if txt and not _is_disambiguation_text(txt):
            norm = normalize_definition2(acr, txt, title_hint=js.get("title"))
            if norm and _accept_expansion(acr, norm):
                out.append((norm, f"{lang}.wiktionary.org", 0.6))
    # opensearch
    if not out:
        api = f"https://{lang}.wiktionary.org/w/api.php"
        data = _http_get_json(api, {"action":"opensearch","format":"json","limit":"6","search":acr})
        if isinstance(data, list) and len(data)>=4:
            titles, descs = data[1], data[2]
            for t, d in list(zip(titles, descs))[:4]:
                txt = (d or t or "").strip()
                norm = normalize_definition2(acr, txt, title_hint=t)
                if norm and _accept_expansion(acr, norm):
                    out.append((norm, f"{lang}.wiktionary.org", 0.56))
    return out



def freedict_lookup(acr: str, lang: str = "en"):
    # Free Dictionary (dictionaryapi.dev): https://api.dictionaryapi.dev/api/v2/entries/en/word
    # Not acronym-focused but sometimes includes expansions in definitions/synonyms.
    url = f"https://api.dictionaryapi.dev/api/v2/entries/{lang}/{_url.quote(acr)}"
    js = _http_get_json(url, {})
    out = []
    try:
        if isinstance(js, list):
            for entry in js[:2]:
                title = (entry.get("word") or "").strip()
                for m in entry.get("meanings") or []:
                    for d in m.get("definitions") or []:
                        text = (d.get("definition") or "").strip()
                        if not text: continue
                        norm = normalize_definition2(acr, text, title_hint=title)
                        if norm and _accept_expansion(acr, norm):
                            out.append((norm, "dictionaryapi.dev", 0.5))
        return out
    except Exception:
        return out



def wordnik_lookup(acr: str):
    key = os.getenv("WORDNIK_API_KEY", "").strip()
    if not key: return []
    url = f"https://api.wordnik.com/v4/word.json/{_url.quote(acr)}/definitions"
    js = _http_get_json(url, {"limit":"5","includeRelated":"false","useCanonical":"false","api_key":key})
    out = []
    if isinstance(js, list):
        for d in js[:5]:
            txt = (d.get("text") or "").strip()
            norm = normalize_definition2(acr, txt, title_hint=d.get("word"))
            if norm and _accept_expansion(acr, norm):
                out.append((norm, "wordnik", 0.48))
    return out



def merriam_webster_lookup(acr: str):
    key = os.getenv("MW_COLLEGIATE_KEY", "").strip()
    if not key: return []
    url = f"https://www.dictionaryapi.com/api/v3/references/collegiate/json/{_url.quote(acr)}"
    js = _http_get_json(url, {"key": key})
    out = []
    if isinstance(js, list):
        for ent in js[:4]:
            if not isinstance(ent, dict): continue
            defs = ent.get("shortdef") or []
            title = ent.get("hwi",{}).get("hw") or ent.get("meta",{}).get("id") or acr
            for s in defs[:3]:
                norm = normalize_definition2(acr, s, title_hint=title)
                if norm and _accept_expansion(acr, norm):
                    out.append((norm, "m-w", 0.55))
    return out



def wordsapi_lookup(acr: str):
    key = os.getenv("WORDSAPI_RAPID_KEY", "").strip()
    host = os.getenv("WORDSAPI_RAPID_HOST", "wordsapiv1.p.rapidapi.com").strip()
    if not key: return []
    url = f"https://{host}/words/{_url.quote(acr)}"
    try:
        # _http_get_json doesn't support custom headers; inline request
        import httpx
from functools import lru_cache
CACHE = {} as _hx
        headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": host}
        with _hx.Client(timeout=10.0) as client:
            r = client.get(url, headers=headers)
            if r.status_code != 200: return []
            js = r.json()
        out = []
        # WordsAPI shape varies; try results of "results"->"definition"
        for res in js.get("results") or []:
            txt = (res.get("definition") or "").strip()
            if not txt: continue
            norm = normalize_definition2(acr, txt, title_hint=js.get("word"))
            if norm and _accept_expansion(acr, norm):
                out.append((norm, "wordsapi", 0.5))
        return out
    except Exception:
        return []



def definitions_net_lookup(acr: str):
    uid = os.getenv("DEFINITIONS_NET_UID","").strip()
    token = os.getenv("DEFINITIONS_NET_TOKEN","").strip()
    if not uid or not token: return []
    url = "https://www.stands4.com/services/v2/defs.php"
    js = _http_get_json(url, {"uid":uid, "tokenid":token, "term":acr, "format":"json"})
    out = []
    try:
        res = (js.get("result") or {}).get("def") or []
        if isinstance(res, dict): res = [res]
        for d in res[:6]:
            txt = (d.get("definition") or "").strip()
            norm = normalize_definition2(acr, txt, title_hint=d.get("term") or acr)
            if norm and _accept_expansion(acr, norm):
                out.append((norm, "definitions.net", 0.5))
        return out
    except Exception:
        return []



def wikipedia_search_titles(acr: str, keyword: str|None=None, *, lang: str='en', limit: int=6):
    # Use opensearch to get candidate page titles, then fetch summary for each title.
    base = f"https://{lang}.wikipedia.org/w/api.php"
    data = _http_get_json(base, {"action":"opensearch","limit":str(limit),"namespace":"0","format":"json","search":acr})
    out = []
    try:
        if isinstance(data, list) and len(data) >= 4:
            titles = data[1]
            for t in titles[:limit]:
                if not t: continue
                summ = wikipedia_rest_summary_title(t, lang=lang)
                if summ:
                    norm = normalize_definition2(acr, summ.get("extract") or "", title_hint=summ.get("title"))
                    if norm and _accept_expansion(acr, norm):
                        out.append((norm, f"{lang}.wikipedia.org", 0.64))
        return out
    except Exception:
        return out

def wikipedia_rest_summary_title(title: str, *, lang: str='en'):
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ','_')}"
    try:
        return _http_get_json(url, {})
    except Exception:
        return {}


def _cached_get(url: str, params: dict[str,str] | None=None, timeout: float=5.0):
    key = url + "?" + "&".join(sorted((params or {}).items()))
    if key in CACHE: 
        return CACHE[key]
    js = _http_get_json(url, params or {})
    CACHE[key] = js
    return js

def wikipedia_extract_strict(acr: str, title: str, extract: str):
    # Try to find an expansion like 'Search engine optimization (SEO)' or reverse
    txt = (extract or "").strip()
    # 1) Parenthetical pattern
    m = re.search(r"([A-Z][A-Za-z][^()]{2,80})\s*\(\s*"+re.escape(acr)+r"\s*\)", txt)
    if m and _accept_expansion(acr, m.group(1), strict=True):
        return m.group(1)
    # 2) Reverse: 'SEO (Search engine optimization)'
    m = re.search(re.escape(acr)+r"\s*\(\s*([^()]{2,80})\)", txt)
    if m and _accept_expansion(acr, m.group(1), strict=True):
        return m.group(1)
    # 3) First clause up to dash or period
    m = re.match(r"([^\.\–\-]{2,120})", txt)
    if m and _accept_expansion(acr, m.group(1), strict=True):
        return m.group(1)
    return ""

def mdn_glossary_lookup(acr: str):
    try:
        url = "https://developer.mozilla.org/api/v1/search"
        js = _cached_get(url, {"q": acr, "locale":"en-US"})
        out = []
        docs = (js.get("documents") or []) if isinstance(js, dict) else []
        for d in docs[:6]:
            slug = (d.get("slug") or "")
            title = (d.get("title") or "").strip()
            if "glossary" in slug.lower():
                exp = title if _accept_expansion(acr, title, strict=False) else ""
                if exp:
                    out.append((exp, "mdn", 0.68))
        return out
    except Exception:
        return []

def w3c_index_lookup(acr: str):
    common = {
        "HTML":"HyperText Markup Language",
        "CSS":"Cascading Style Sheets",
        "SVG":"Scalable Vector Graphics",
        "ARIA":"Accessible Rich Internet Applications",
        "WCAG":"Web Content Accessibility Guidelines"
    }
    val = common.get(acr.upper())
    return [(val, "w3c", 0.66)] if val else []

def pack_lookup(acr: str):
    import json, os
    base = os.path.join(os.path.dirname(__file__), "packs")
    out = []
    for name,src in (("nist.json","nist"),("nasa.json","nasa")):
        p = os.path.join(base, name)
        try:
            with open(p,"r",encoding="utf-8") as f:
                data = json.load(f)
            val = data.get(acr.upper())
            if val:
                out.append((val, f"pack-{src}", 0.78))
        except Exception:
            pass
    return out

log = logging.getLogger('web-lookup')

def get_web_candidates(acr: str, domain: str|None=None, *, lang: str='en', limit: int=8):
    out = []
    def add(items):
        for d,s,sc in items or []:
            out.append({"definition": d, "source": s, "confidence": float(sc)})
    # Domain-first
    if (domain or '').lower() in ('tech','technology','web','computing'):
        add(pack_lookup(acr)); 
        add(mdn_glossary_lookup(acr)); 
        add(w3c_index_lookup(acr))
    # Generic sources
    add(wikipedia_search_titles(acr, lang=lang, limit=6))
    add(acromine_lookup(acr, limit=6))
    # Deduplicate by definition
    seen = set(); uniq = []
    for c in out:
        key = (c["definition"] or "").strip().lower()
        if not key or key in seen: continue
        seen.add(key); uniq.append(c)
    log.info("web_candidates(%s): %d", acr, len(uniq))
    return uniq[:limit]

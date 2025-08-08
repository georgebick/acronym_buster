import os
import re
import requests
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlencode

BING_KEY = os.getenv("BING_SUBSCRIPTION_KEY")
BING_ENDPOINT = os.getenv("BING_ENDPOINT", "https://api.bing.microsoft.com/v7.0/search")
WEB_MAX_CANDIDATES = int(os.getenv("WEB_MAX_CANDIDATES", "5"))

def keywords_from_text(text: str, k: int = 8) -> List[str]:
    # naive keyword extraction: take distinct capitalized words & frequent nouns-ish tokens
    words = re.findall(r"[A-Za-z]{3,}", text)
    caps = [w for w in words if w[0].isupper()]
    freq = {}
    for w in words:
        wl = w.lower()
        freq[wl] = freq.get(wl, 0) + 1
    ranked = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return list(dict.fromkeys([*caps[:k//2], *[w for w,_ in ranked[:k]]]))[:k]

def wikipedia_search(acr: str) -> Optional[Tuple[str, str, float]]:
    # Use opensearch API
    q = f"{acr} acronym"
    url = f"https://en.wikipedia.org/w/api.php?action=opensearch&limit=5&namespace=0&format=json&search={requests.utils.quote(q)}"
    try:
        r = requests.get(url, timeout=6)
        if r.ok:
            data = r.json()
            titles, descs, links = data[1], data[2], data[3]
            for t, d, l in zip(titles, descs, links):
                if len(t) <= 80 and acr.upper() in t.upper():
                    # Description often contains "X is ... stands for ..." etc.
                    if d:
                        return (t, "en.wikipedia.org", 0.55)
            # fallback first result
            if titles:
                return (titles[0], "en.wikipedia.org", 0.5)
    except Exception:
        pass
    return None

def duckduckgo_instant(acr: str) -> Optional[Tuple[str, str, float]]:
    url = "https://api.duckduckgo.com/"
    params = {"q": f"{acr} stands for", "format": "json", "no_html": 1, "skip_disambig": 1}
    try:
        r = requests.get(url, params=params, timeout=6)
        if r.ok:
            data = r.json()
            abstract = data.get("AbstractText") or ""
            if abstract:
                # crude: take the first clause
                snippet = abstract.split(".")[0].strip()
                if snippet and len(snippet) > 3:
                    return (snippet, "duckduckgo.com", 0.5)
            # try related topics
            topics = data.get("RelatedTopics") or []
            for t in topics:
                if isinstance(t, dict) and t.get("Text"):
                    txt = t["Text"].split(".")[0].strip()
                    if acr.upper() in txt.upper():
                        return (txt, "duckduckgo.com", 0.48)
    except Exception:
        pass
    return None

def bing_search(acr: str, keywords: List[str]) -> Optional[Tuple[str, str, float]]:
    if not BING_KEY:
        return None
    headers = {"Ocp-Apim-Subscription-Key": BING_KEY}
    params = {"q": f"{acr} acronym meaning {' '.join(keywords)}", "mkt": "en-GB", "count": WEB_MAX_CANDIDATES}
    try:
        r = requests.get(BING_ENDPOINT, headers=headers, params=params, timeout=6)
        if r.ok:
            data = r.json()
            web_pages = (data.get("webPages") or {}).get("value", [])
            for item in web_pages:
                name = item.get("name", "")
                snippet = item.get("snippet", "")
                url = item.get("url", "")
                domain = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
                text = snippet or name
                if len(text) > 3:
                    return (text[:180], domain, 0.58)
    except Exception:
        pass
    return None

def web_fallback(acr: str, context_text: str) -> Optional[Tuple[str, str, float]]:
    kws = keywords_from_text(context_text, k=8)
    # Prefer Bing if key, else Wikipedia, then DDG
    for fn in (lambda: bing_search(acr, kws), lambda: wikipedia_search(acr), lambda: duckduckgo_instant(acr)):
        res = fn()
        if res:
            return res
    return None


def wikipedia_candidates_loose(acr: str) -> List[Tuple[str,str,float]]:
    # Try without 'acronym' to pull common expansions by page title
    q = acr
    url = f"https://en.wikipedia.org/w/api.php?action=opensearch&limit=8&namespace=0&format=json&search={requests.utils.quote(q)}"
    out = []
    try:
        r = requests.get(url, timeout=6)
        if r.ok:
            data = r.json()
            titles, descs, links = data[1], data[2], data[3]
            for t, d, l in zip(titles, descs, links):
                if t and len(t) <= 120:
                    out.append((t, "en.wikipedia.org", 0.5))
    except Exception:
        pass
    return out

def wiktionary_candidates(acr: str) -> List[Tuple[str,str,float]]:
    # Very simple: search page titles; Wiktionary often lists expansions in summaries
    api = "https://en.wiktionary.org/w/api.php"
    params = {"action":"opensearch", "format":"json", "limit":"5", "search":acr}
    out = []
    try:
        r = requests.get(api, params=params, timeout=6)
        if r.ok:
            data = r.json()
            titles = data[1]
            for t in titles:
                if t and len(t) <= 120:
                    out.append((t, "en.wiktionary.org", 0.45))
    except Exception:
        pass
    return out

def web_candidates(acr: str, context_text: str, limit: int = 5) -> List[Tuple[str,str,float]]:
    seen = set()
    out = []
    # Try multiple free sources; keep first 'limit' unique strings
    sources = [lambda: wikipedia_candidates(acr),
               lambda: wikipedia_candidates_loose(acr),
               lambda: duckduckgo_candidates(acr),
               lambda: wiktionary_candidates(acr)]
    for fn in sources:
        try:
            for defn, dom, sc in (fn() or []):
                key = (defn, dom)
                if key in seen: continue
                seen.add(key)
                out.append((defn, dom, min(0.6, sc)))
                if len(out) >= limit:
                    return out
        except Exception:
            continue
    return out

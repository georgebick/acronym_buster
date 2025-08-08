import re
from typing import List, Dict, Tuple, Optional
from collections import defaultdict
from rapidfuzz.distance import Levenshtein

def normalize_acronym(raw: str) -> str:
    # Strip trailing possessive and plural 's
    raw = APOSTROPHE_RE.sub('', raw)
    raw = raw.rstrip('sS')
    # Remove dots in dotted acronyms: A.I. -> AI
    if '.' in raw:
        raw = raw.replace('.', '')
    return raw.upper()

ACR_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}(?:[-/][A-Z0-9]{2,9})?)s?\b")
# Common false positives / context-dependent terms. Expand as needed.
STOPLIST = set('''
AM PM
MON TUE WED THU FRI SAT SUN
JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC
'''.split())

INCLUDE_COMMON = True  # can be toggled from env in main

DOT_ACR_RE = re.compile(r"([A-Za-z](?:\.[A-Za-z])+\.)")  # e.g., A.I., U.S.A.
APOSTROPHE_RE = re.compile(r"[’']s")  # strip possessive

SPLIT_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")  # simple sentence splitter

def sentence_split(text: str) -> List[str]:
    # Normalize whitespace, then split.
    text = re.sub(r"\s+", " ", text.replace("\n", " ").strip())
    if not text:
        return []
    sentences = SPLIT_SENT_RE.split(text)
    return [s.strip() for s in sentences if s.strip()]

def find_acronym_candidates(text: str) -> List[str]:
    found = []
    for m in ACR_RE.finditer(text):
        term = m.group(1)
        base = normalize_acronym(term)
        if INCLUDE_COMMON is False and base in STOPLIST:
            continue
        if len(base) < 2:
            continue
        found.append(base)
    # dedupe preserving order
    seen = set()
    deduped = []
    for a in found:
        if a not in seen:
            seen.add(a)
            deduped.append(a)
    return deduped

def _initials(s: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", s)
    return ''.join(t[0].upper() for t in tokens if t)

def initials_alignment_score(acr: str, phrase: str) -> float:
    ini = _initials(phrase)
    if not ini:
        return 0.0
    # proportion of matching leading characters
    prefix = 0
    for a, b in zip(acr, ini):
        if a == b:
            prefix += 1
        else:
            break
    # Levenshtein on initials for robustness
    lev = Levenshtein.normalized_similarity(acr, ini)
    # combine
    score = 0.6 * (prefix / max(1, len(acr))) + 0.4 * lev
    # small penalty for very long phrases
    penalty = min(0.2, max(0, (len(phrase.split()) - len(acr)) * 0.02))
    return max(0.0, min(1.0, score - penalty))

def _search_window(sentences: List[str], idx: int, span: int = 3) -> str:
    start = max(0, idx - span)
    end = min(len(sentences), idx + span + 1)
    return ' '.join(sentences[start:end])

def find_definition_in_text(acr: str, sentences: List[str]) -> Optional[Tuple[str, float, str]]:
    # Patterns around first occurrence
    # 1) Long form (ACR)
    pat1 = re.compile(rf"\b([A-Z][a-z][\w ,-/&]+?)\s*\(\s*{re.escape(acr)}\s*\)")
    # 2) ACR (long form)
    pat2 = re.compile(rf"\b{re.escape(acr)}\s*\(\s*([A-Z][a-z][\w ,-/&]+?)\s*\)")
    # 3) ACR - long form / : long form / , short for long form / stands for
    pat3a = re.compile(rf"\b{re.escape(acr)}\s*[-:–]\s*([A-Z][a-z][\w ,-/&]+)")
    pat3b = re.compile(rf"\b{re.escape(acr)}\s*,\s*(?:short for|stands for)\s+([A-Z][a-z][\w ,-/&]+)", re.IGNORECASE)

    for i, s in enumerate(sentences):
        if acr in s:
            window = _search_window(sentences, i, span=1)
            # Check patterns in window text
            for rx, base_score in [(pat1, 0.95), (pat2, 0.9), (pat3a, 0.85), (pat3b, 0.85)]:
                m = rx.search(window)
                if m:
                    phrase = m.group(1).strip().strip(' .;:,')
                    align = initials_alignment_score(acr, phrase)
                    if align >= 0.6 or len(acr) <= 3:  # tolerate short acronyms
                        conf = min(0.98, base_score * (0.8 + 0.2 * align))
                        return (phrase, conf, window)
            # Fallback: look across this and next 2 sentences
            pos = s.find(acr)
            context = ' '.join([sentences[i]] + sentences[i+1:i+3])
            tail = context[pos + len(acr):].strip()
            tail_words = ' '.join(tail.split()[:10])
            if tail_words:
                align = initials_alignment_score(acr, tail_words)
                if align >= 0.65:
                    conf = 0.66 + 0.25 * (align - 0.65)
                    return (tail_words.strip(' .;:,'), min(0.9, conf), context.strip())
    return None

def scan_tables_for_glossary(doc) -> Dict[str, str]:
    glossary = {}
    for table in getattr(doc, 'tables', []):
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            # Pattern search inside any cell: Long form (ACR)
            joined = ' | '.join(cells)
            longforms = collect_global_longforms(joined)
            for acr, longf in longforms.items():
                glossary.setdefault(acr, longf)
            # Pairwise first two cols if present
            if len(cells) >= 2:
                a, b = cells[0], cells[1]
                for left, right in [(a,b),(b,a)]:
                    m = ACR_RE.fullmatch(left.strip())
                    if m:
                        term = normalize_acronym(m.group(1))
                        if term and (INCLUDE_COMMON or term not in STOPLIST) and right and len(right) > 2:
                            glossary.setdefault(term, right.strip())
    return glossary


def collect_global_longforms(text: str) -> Dict[str, str]:
    # Find any 'Long form (ACR)' or 'ACR (Long form)' across the whole doc
    mappings: Dict[str, str] = {}
    # Long form (ACR)
    rx1 = re.compile(r"\b([A-Z][a-z][\w ,-/&]+?)\s*\(\s*([A-Z][A-Z0-9]{1,9})\s*\)")
    # ACR (Long form)
    rx2 = re.compile(r"\b([A-Z][A-Z0-9]{1,9})\s*\(\s*([A-Z][a-z][\w ,-/&]+?)\s*\)")
    for m in rx1.finditer(text):
        longf, acr = m.group(1).strip(), normalize_acronym(m.group(2))
        if acr and acr not in mappings:
            mappings[acr] = longf
    for m in rx2.finditer(text):
        acr, longf = normalize_acronym(m.group(1)), m.group(2).strip()
        if acr and acr not in mappings:
            mappings[acr] = longf
    return mappings

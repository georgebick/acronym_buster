import re
from typing import List, Dict, Tuple, Optional
from collections import defaultdict
from rapidfuzz.distance import Levenshtein

ACR_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}(?:[-/][A-Z0-9]{2,9})?)s?\b")
# Common false positives / context-dependent terms. Expand as needed.
STOPLIST = set('''
AM PM IT ID TV AI UK USA EU US ASAP FYI ETA DIY VAT HR CEO CFO CTO CIO FAQ ERP CRM CAD CAM
PDF DOCX CSV JSON HTML CSS JS API KPI R&D QA QC NDA SLA TBC TBA TBD
MON TUE WED THU FRI SAT SUN JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC
'''.split())

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
        base = term.rstrip('s')  # normalize plurals
        if base.upper() in STOPLIST:
            continue
        # avoid single-letter
        if len(base) < 2:
            continue
        found.append(base.upper())
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

def _search_window(sentences: List[str], idx: int, span: int = 2) -> str:
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
            # Fallback: initials alignment with next noun-ish phrase (heuristic)
            # Take the sentence text after the acronym and grab up to 8 words
            pos = s.find(acr)
            tail = s[pos + len(acr):].strip()
            tail_words = ' '.join(tail.split()[:8])
            if tail_words:
                align = initials_alignment_score(acr, tail_words)
                if align >= 0.7:
                    conf = 0.68 + 0.2 * (align - 0.7)  # ~0.68..0.88
                    return (tail_words.strip(' .;:,'), conf, s.strip())
    return None

def scan_tables_for_glossary(doc) -> Dict[str, str]:
    # doc is a python-docx Document
    glossary = {}
    for table in getattr(doc, 'tables', []):
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if len(cells) < 2:
                continue
            left, right = cells[0], cells[1]
            # Try both orientations
            for a, d in [(left, right), (right, left)]:
                m = ACR_RE.fullmatch(a.strip())
                if m:
                    term = m.group(1).rstrip('s').upper()
                    if term and term not in STOPLIST and d and len(d) > 2:
                        glossary[term] = d.strip()
    return glossary

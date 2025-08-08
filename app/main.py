import io
import csv
import os
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel
from docx import Document as DocxDoc

from .models import ExtractionResponse, AcronymResult
from .extraction import sentence_split, find_acronym_candidates, find_definition_in_text, scan_tables_for_glossary, collect_global_longforms, INCLUDE_COMMON
from .web_lookup import web_fallback
from dotenv import load_dotenv
import logging

load_dotenv()

# Toggle common acronyms via env var INCLUDE_COMMON=true/false
from os import getenv
val = (getenv('INCLUDE_COMMON') or 'true').strip().lower()
try:
    import app.extraction as ex
    ex.INCLUDE_COMMON = val in ('1','true','yes','y','on')
except Exception:
    pass

# Logging
DBG = (getenv('DEBUG') or 'false').strip().lower() in ('1','true','yes','y','on')
logging.basicConfig(level=logging.DEBUG if DBG else logging.INFO)
logger = logging.getLogger('acronym-extractor')

app = FastAPI(title="Acronym Extractor")


@app.get("/health")
def health():
    return {"status": "ok"}

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/extract", response_model=ExtractionResponse)
async def extract(file: UploadFile = File(...)) -> ExtractionResponse:
    if not file.filename.lower().endswith(".docx"):
        return ExtractionResponse(acronyms=[])

    # Read docx into python-docx
    contents = await file.read()
    f = io.BytesIO(contents)
    doc = DocxDoc(f)

    # Extract paragraphs text
    text_parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    # Footnotes (python-docx: doc.part.footnotes may exist)
    try:
        fnotes = []
        fn = getattr(doc.part, 'footnotes', None)
        if fn is not None:
            for f in fn.part.element.xpath('//w:footnote//w:t', namespaces={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}):
                if f.text and f.text.strip():
                    fnotes.append(f.text.strip())
        foot_text = '\n'.join(fnotes)
    except Exception:
        foot_text = ''

    full_text = "\n".join(text_parts + ([foot_text] if foot_text else []))
    sentences = sentence_split(full_text)

    # From tables, get glossary pairs
    glossary = scan_tables_for_glossary(doc)

    # Global longforms anywhere in the doc
    global_map = collect_global_longforms(full_text)
    for acr, longf in global_map.items():
        glossary.setdefault(acr, longf)

    acronyms = find_acronym_candidates(full_text + "\n" + "\n".join(glossary.keys()))

    
    results = []
    for acr in acronyms:
        cands: list[Candidate] = []
        chosen_idx = 0

        # 1) From glossary/table
        if acr in glossary:
            defn = glossary[acr]
            cands.append(Candidate(definition=defn, confidence=0.86, source='document'))

        # 2) From in-text
        hit = find_definition_in_text(acr, sentences)
        if hit:
            phrase, conf, excerpt = hit
            cands.append(Candidate(definition=phrase, confidence=round(conf, 3), source='document'))
        else:
            excerpt = None

        # 3) Canonical known-good
        if acr in CANONICAL_MAP:
            cands.append(Candidate(definition=CANONICAL_MAP[acr], confidence=0.84, source='canonical'))

        # 4) Web candidates (free)
        try:
            wc = web_candidates(acr, full_text[:4000], limit=5)
            for defn, dom, sc in (wc or []):
                cands.append(Candidate(definition=defn, confidence=round(sc, 3), source=f'web:{dom}'))
        except Exception as e:
            logger.warning(f'web_candidates error for {acr}: {e}')

        # 5) De-duplicate by definition text
        seen_defs = set()
        unique = []
        for c in cands:
            key = c.definition.strip().lower()
            if key not in seen_defs:
                seen_defs.add(key)
                unique.append(c)
        cands = unique

        # 6) LAST-RESORT fallback so the picker is never empty
        if not cands:
            if acr in CANONICAL_MAP:
                cands = [Candidate(definition=CANONICAL_MAP[acr], confidence=0.5, source='canonical')]
            else:
                cands = [Candidate(definition='(no definition found)', confidence=0.0, source='none')]

        # 7) Pick chosen index: prefer document > canonical > web
        chosen_idx = 0
        for i, c in enumerate(cands):
            if c.source == 'document':
                chosen_idx = i; break
        else:
            for i, c in enumerate(cands):
                if c.source == 'canonical':
                    chosen_idx = i; break

        chosen = cands[chosen_idx]
        results.append(AcronymResult(
            term=acr,
            definition=chosen.definition,
            confidence=chosen.confidence,
            source=chosen.source,
            note=('possible match (web)' if chosen.source.startswith('web:') else None),
            first_seen_excerpt=(excerpt[:240] if excerpt else None),
            candidates=cands,
            chosen_index=chosen_idx
        ))

    return ExtractionResponse(acronyms=results)


@app.post("/extract-csv")
async def extract_csv(file: UploadFile = File(...)):
    res: ExtractionResponse = await extract(file)  # reuse logic
    # Build CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Acronym", "Definition", "Confidence", "Source", "Note", "FirstSeenExcerpt"])
    for item in res.acronyms:
        writer.writerow([item.term, item.definition or "", item.confidence, item.source, item.note or "", item.first_seen_excerpt or ""])
    output.seek(0)

    headers = {"Content-Disposition": "attachment; filename=acronyms.csv"}
    return StreamingResponse(io.BytesIO(output.getvalue().encode("utf-8")), media_type="text/csv", headers=headers)

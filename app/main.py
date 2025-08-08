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
from .extraction import sentence_split, find_acronym_candidates, find_definition_in_text, scan_tables_for_glossary
from .web_lookup import web_fallback
from dotenv import load_dotenv

load_dotenv()

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
    full_text = "\n".join(text_parts)
    sentences = sentence_split(full_text)

    # From tables, get glossary pairs
    glossary = scan_tables_for_glossary(doc)

    acronyms = find_acronym_candidates(full_text + "\n" + "\n".join(glossary.keys()))

    results = []
    for acr in acronyms:
        # First, see if glossary has it
        if acr in glossary:
            results.append(AcronymResult(
                term=acr,
                definition=glossary[acr],
                confidence=0.86,
                source="document",
                first_seen_excerpt=f"{acr} – {glossary[acr]} (table)"
            ))
            continue

        # Try in-text patterns
        hit = find_definition_in_text(acr, sentences)
        if hit:
            phrase, conf, excerpt = hit
            results.append(AcronymResult(
                term=acr,
                definition=phrase,
                confidence=round(conf, 3),
                source="document",
                first_seen_excerpt=excerpt[:240]
            ))
            continue

        # Web fallback
        web = web_fallback(acr, full_text[:4000])
        if web:
            phrase, domain, score = web
            results.append(AcronymResult(
                term=acr,
                definition=phrase,
                confidence=round(min(0.6, score), 3),
                source=f"web:{domain}",
                note="possible match (web)",
                first_seen_excerpt=None
            ))
        else:
            results.append(AcronymResult(term=acr))

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

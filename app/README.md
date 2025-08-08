# Acronym Extractor Web App

Upload a `.docx` (Word) document and get a table of **acronyms/abbreviations** and their **definitions**.

- Definitions are sourced from the **document** first.
- If a definition isn't found, the app performs a **web lookup** (Wikipedia / DuckDuckGo / optional Bing) and marks the result as **"possible match (web)"**.
- Export results to CSV.

## Quick start

### 1) Create and activate a virtual environment
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
```

### 2) Install dependencies
```bash
pip install -r requirements.txt
```

### 3) Configure optional API keys
Copy `.env.example` to `.env` and set keys as needed. You don't need any keys for Wikipedia/DuckDuckGo but Bing improves web matches.

```bash
cp .env.example .env
# then edit .env
```

### 4) Run the app
```bash
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000 in your browser.

## Notes

- Only `.docx` is supported in v1. (You can extend with pdf/ocr later.)
- The web lookups send only the **acronym** and a few **generic keywords** from your document (not full text) for privacy.
- The app does not store document content on disk; it processes in memory.

## Tests
```bash
pytest -q
```

## Project structure
```
app/
  main.py            # FastAPI app and routes
  extraction.py      # Acronym detection & in-doc definition search
  web_lookup.py      # Web fallback providers and heuristics
  models.py          # Pydantic models
  templates/index.html
  static/style.css
tests/
  test_extraction.py
requirements.txt
.env.example
README.md
```


---

## Deploy to Render

1. Create a new **Web Service** on Render and connect this repo (or upload the ZIP).
2. When prompted, set:
   - **Runtime**: Python
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. (Optional) Add environment variable `BING_SUBSCRIPTION_KEY` for better web definitions.
4. Deploy. Your app will be available at `https://<your-service-name>.onrender.com`.

Alternatively, use the included `render.yaml` if you prefer Infrastructure-as-code.


### New (user choice)
- Backend now returns multiple **candidates** per acronym (document, canonical, Wikipedia/DDG).
- UI shows a per-row **dropdown** to pick the correct definition before downloading CSV.
- CSV is built client-side from your selections.


### v3 improvements
- Guaranteed **candidates** per acronym (never empty): document, canonical, web; fallback placeholder if none found.
- Added **free web sources**: Wikipedia (two modes), DuckDuckGo, Wiktionary. All with timeouts and graceful failures.
- **DEBUG** mode: set `DEBUG=true` (Render env var) to see web lookup logs in the service logs.

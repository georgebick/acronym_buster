# Acronym Buster (v4.4.1-clean)

## Run locally
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Render
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Ensure only one entrypoint: `app/main.py`
- Clear Build Cache if issues persist.

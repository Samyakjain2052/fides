"""
PII Detection Microservice — FastAPI wrapper around Presidio + custom
Indian identifier recognizers (recognizers.py).

Run:
    uvicorn main:app --reload --port 8001
"""

from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional

from recognizers import get_analyzer

app = FastAPI(title="PII Detection Microservice", version="0.1.0")

# Analyzer is built once at startup — reused across requests (expensive to rebuild)
analyzer = get_analyzer()


# ---------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------

class DetectRequest(BaseModel):
    text: str
    language: str = "en"


class Detection(BaseModel):
    type: str
    value: str
    start: int
    end: int
    score: float


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------

@app.get("/health")
def health():
    """Simple liveness check — Fides/other services can poll this."""
    return {"status": "ok"}


@app.post("/detect", response_model=List[Detection])
def detect(request: DetectRequest):
    """
    Detects PII entities (IN_PAN, IN_AADHAAR, + Presidio's built-in
    entities like PERSON, EMAIL_ADDRESS, PHONE_NUMBER) in the given text.
    """
    results = analyzer.analyze(text=request.text, language=request.language)

    return [
        Detection(
            type=r.entity_type,
            value=request.text[r.start:r.end],
            start=r.start,
            end=r.end,
            score=round(r.score, 2),
        )
        for r in results
    ]

from pydantic import BaseModel, Field
from typing import Optional, List

class Candidate(BaseModel):
    definition: str
    confidence: float
    source: str

class AcronymResult(BaseModel):
    term: str
    definition: Optional[str] = None
    confidence: float = 0.0
    source: str = "none"  # 'document' | 'web:domain' | 'none'
    note: Optional[str] = None
    first_seen_excerpt: Optional[str] = None
    candidates: List[Candidate] = Field(default_factory=list)
    chosen_index: int = 0

class ExtractionResponse(BaseModel):
    acronyms: List[AcronymResult] = Field(default_factory=list)

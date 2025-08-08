from pydantic import BaseModel, Field
from typing import Optional, List

class AcronymResult(BaseModel):
    term: str
    definition: Optional[str] = None
    confidence: float = 0.0
    source: str = "none"  # 'document' | 'web:domain' | 'none'
    note: Optional[str] = None
    first_seen_excerpt: Optional[str] = None

class ExtractionResponse(BaseModel):
    acronyms: List[AcronymResult] = Field(default_factory=list)

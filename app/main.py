"""Archive quote-alignment API."""

from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator

from .align import (
    MAX_ALLOWED_EDITS,
    align_excerpts,
    align_quotes,
    align_quotes_fuzzy,
    attribute_quotes,
)

app = FastAPI(title="Archive Quote Alignment API", version="1.3.0")


class AlignRequest(BaseModel):
    text: str = Field(..., description="Original source text")
    quotes: list[str] = Field(
        ..., description="Quotes to locate, matched in array order"
    )


class FuzzyQuote(BaseModel):
    text: str = Field(..., description="Quote text to locate")
    max_edits: int = Field(
        ...,
        ge=0,
        le=MAX_ALLOWED_EDITS,
        description="Maximum allowed Levenshtein distance (0-2)",
    )


class FuzzyAlignRequest(BaseModel):
    text: str = Field(..., description="Original source text")
    quotes: list[FuzzyQuote] = Field(
        ..., description="Quotes to locate, matched in array order"
    )


class Excerpt(BaseModel):
    fragments: list[str] = Field(
        ...,
        min_length=2,
        max_length=5,
        description="Ordered fragments of the excerpt (2-5, each non-blank)",
    )
    max_gap: int = Field(
        ...,
        ge=0,
        le=10000,
        description=(
            "Maximum gap in original characters between adjacent "
            "fragments (0-10000)"
        ),
    )

    @field_validator("fragments")
    @classmethod
    def fragments_must_be_non_blank(cls, fragments: list[str]) -> list[str]:
        if any(not fragment.strip() for fragment in fragments):
            raise ValueError("fragments must be non-blank")
        return fragments


class AlignExcerptsRequest(BaseModel):
    text: str = Field(..., description="Original source text")
    excerpts: list[Excerpt] = Field(
        ..., description="Excerpts to locate, matched in array order"
    )


class AttributeDocument(BaseModel):
    id: str = Field(..., description="Unique identifier of the document")
    text: str = Field(..., description="Original source text of the document")


class AttributeRequest(BaseModel):
    documents: list[AttributeDocument] = Field(
        ...,
        min_length=2,
        max_length=4,
        description="2-4 source documents with unique ids",
    )
    quotes: list[FuzzyQuote] = Field(
        ..., description="Quotes to attribute, matched in array order"
    )

    @field_validator("documents")
    @classmethod
    def document_ids_must_be_unique(
        cls, documents: list[AttributeDocument]
    ) -> list[AttributeDocument]:
        ids = [document.id for document in documents]
        if len(set(ids)) != len(ids):
            raise ValueError("document ids must be unique")
        return documents


class Span(BaseModel):
    start: int = Field(..., description="Start offset in the original text")
    end: int = Field(..., description="End offset (exclusive) in the original text")


class FuzzySpan(BaseModel):
    start: int = Field(..., description="Start offset in the original text")
    end: int = Field(..., description="End offset (exclusive) in the original text")
    edit_distance: int = Field(
        ..., description="Levenshtein distance between the quote and the span"
    )


class Attribution(BaseModel):
    document_id: str = Field(
        ..., description="Id of the document the quote is attributed to"
    )
    start: int = Field(
        ..., description="Start offset in the document's original text"
    )
    end: int = Field(
        ..., description="End offset (exclusive) in the document's original text"
    )
    edit_distance: int = Field(
        ..., description="Levenshtein distance between the quote and the span"
    )


class AlignResponse(BaseModel):
    results: list[Span | None] = Field(
        ..., description="One entry per quote; null when the quote is not located"
    )


class FuzzyAlignResponse(BaseModel):
    results: list[FuzzySpan | None] = Field(
        ..., description="One entry per quote; null when the quote is not located"
    )


class ExcerptAlignment(BaseModel):
    start: int = Field(
        ..., description="Start offset of the excerpt in the original text"
    )
    end: int = Field(
        ...,
        description=(
            "End offset (exclusive) of the excerpt in the original text"
        ),
    )
    fragment_spans: list[Span] = Field(
        ..., description="Located span of each fragment, in fragment order"
    )
    omitted_ranges: list[Span] = Field(
        ...,
        description=(
            "Non-empty original-text ranges omitted between adjacent "
            "fragments"
        ),
    )


class AlignExcerptsResponse(BaseModel):
    results: list[ExcerptAlignment | None] = Field(
        ...,
        description="One entry per excerpt; null when the excerpt is not located",
    )


class AttributeResponse(BaseModel):
    results: list[Attribution | None] = Field(
        ..., description="One entry per quote; null when the quote is not attributed"
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/quotes/align", response_model=AlignResponse)
def align(request: AlignRequest) -> AlignResponse:
    spans = align_quotes(request.text, request.quotes)
    return AlignResponse(
        results=[
            Span(start=span[0], end=span[1]) if span is not None else None
            for span in spans
        ]
    )


@app.post("/quotes/align-fuzzy", response_model=FuzzyAlignResponse)
def align_fuzzy(request: FuzzyAlignRequest) -> FuzzyAlignResponse:
    spans = align_quotes_fuzzy(
        request.text, [(quote.text, quote.max_edits) for quote in request.quotes]
    )
    return FuzzyAlignResponse(
        results=[
            FuzzySpan(start=span[0], end=span[1], edit_distance=span[2])
            if span is not None
            else None
            for span in spans
        ]
    )


@app.post("/quotes/align-excerpts", response_model=AlignExcerptsResponse)
def align_excerpts_endpoint(
    request: AlignExcerptsRequest,
) -> AlignExcerptsResponse:
    aligned = align_excerpts(
        request.text,
        [(excerpt.fragments, excerpt.max_gap) for excerpt in request.excerpts],
    )
    return AlignExcerptsResponse(
        results=[
            ExcerptAlignment(
                start=match[0],
                end=match[1],
                fragment_spans=[
                    Span(start=start, end=end) for start, end in match[2]
                ],
                omitted_ranges=[
                    Span(start=start, end=end) for start, end in match[3]
                ],
            )
            if match is not None
            else None
            for match in aligned
        ]
    )


@app.post("/quotes/attribute", response_model=AttributeResponse)
def attribute(request: AttributeRequest) -> AttributeResponse:
    spans = attribute_quotes(
        [document.text for document in request.documents],
        [(quote.text, quote.max_edits) for quote in request.quotes],
    )
    return AttributeResponse(
        results=[
            Attribution(
                document_id=request.documents[span[0]].id,
                start=span[1],
                end=span[2],
                edit_distance=span[3],
            )
            if span is not None
            else None
            for span in spans
        ]
    )

"""KYC & Client Onboarding Document Agent — FastAPI service.

Create an onboarding case for a client, upload their documents, and
four LangGraph agents check them: ingestion classifies and extracts,
retrieval pulls the KYC requirement rules, validation cross-checks
fields across documents and against the declared application facts, and
the supervisor routes the case — clean cases pass straight through,
anything with a mismatch goes to a human reviewer. A rejection is only
ever a *proposal* until a human reviewer confirms it.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.models import (
    CaseCreate,
    CaseStatusResponse,
    CaseSummary,
    DocumentInput,
    MetricsResponse,
    OnboardingCase,
    ReviewRequest,
)
from app.pipeline import OnboardingPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

pipeline: OnboardingPipeline | None = None

_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pipeline
    pipeline = OnboardingPipeline()
    logger.info("KYC Onboarding Agent started (LLM provider: %s)",
                pipeline.llm.model)
    yield


app = FastAPI(
    title="KYC & Client Onboarding Document Agent",
    description=(
        "Multi-agent KYC onboarding: classify documents, extract and "
        "validate fields, and flag mismatches for a human reviewer. Clean "
        "cases pass straight through; the rest reach the review queue with "
        "the exact discrepancy highlighted. Built with FastAPI, LangGraph "
        "and Azure OpenAI."
    ),
    version="1.0.0",
    lifespan=lifespan,
    # Self-hosted API docs (no CDN dependency — /docs works offline).
    # Swagger UI assets are vendored under app/static/swagger-ui/.
    docs_url=None,
    redoc_url=None,
)

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/docs", include_in_schema=False, tags=["service"])
def custom_swagger_ui() -> HTMLResponse:
    """Swagger UI, served from vendored local assets (works offline)."""
    return HTMLResponse(
        """
        <!DOCTYPE html>
        <html>
        <head>
          <link type="text/css" rel="stylesheet"
                href="/static/swagger-ui/swagger-ui.css">
          <title>KYC & Client Onboarding Document Agent - Swagger UI</title>
        </head>
        <body>
          <div id="swagger-ui"></div>
          <script src="/static/swagger-ui/swagger-ui-bundle.js"></script>
          <script src="/static/swagger-ui/swagger-ui-standalone-preset.js"></script>
          <script>
            window.onload = () => {
              window.ui = SwaggerUIBundle({
                url: "/openapi.json",
                dom_id: "#swagger-ui",
                presets: [
                  SwaggerUIBundle.presets.apis,
                  SwaggerUIStandalonePreset
                ],
                layout: "StandaloneLayout"
              });
            };
          </script>
        </body>
        </html>
        """
    )


def _pipeline() -> OnboardingPipeline:
    assert pipeline is not None
    return pipeline


# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False, tags=["service"])
def dashboard() -> FileResponse:
    """User-facing dashboard (vanilla HTML/CSS/JS, no build step)."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/health", tags=["service"])
def health() -> dict:
    return {"status": "ok", "llm_provider": _pipeline().llm.model}


# ---------------------------------------------------------------------------
@app.post("/onboarding", response_model=OnboardingCase, status_code=201,
          tags=["onboarding"])
def create_case(data: CaseCreate) -> OnboardingCase:
    """Create an onboarding case for a new client."""
    return _pipeline().create_case(data)


@app.get("/onboarding", response_model=list[CaseSummary], tags=["onboarding"])
def list_cases(
    status: str | None = Query(default=None, description="Filter by status"),
    client_type: str | None = Query(default=None, description="individual/company"),
) -> list[CaseSummary]:
    cases = _pipeline().list_cases(status=status, client_type=client_type)
    return [
        CaseSummary(
            id=c.id,
            client_name=c.client_name,
            client_type=c.client_type,
            status=c.status,
            decision=c.decision.value if c.decision else None,
            documents_count=len(c.documents),
            mismatches_count=len(c.mismatches),
            requires_hitl=c.requires_hitl,
            created_at=c.created_at,
        )
        for c in cases
    ]


@app.get("/onboarding/review-queue", response_model=list[CaseSummary],
         tags=["onboarding"])
def review_queue() -> list[CaseSummary]:
    """All cases with pending mismatches, oldest first."""
    cases = _pipeline().review_queue()
    return [
        CaseSummary(
            id=c.id,
            client_name=c.client_name,
            client_type=c.client_type,
            status=c.status,
            decision=c.decision.value if c.decision else None,
            documents_count=len(c.documents),
            mismatches_count=len(c.mismatches),
            requires_hitl=c.requires_hitl,
            created_at=c.created_at,
        )
        for c in cases
    ]


@app.get("/onboarding/{case_id}", response_model=OnboardingCase,
         tags=["onboarding"])
def get_case(case_id: str) -> OnboardingCase:
    case = _pipeline().get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.get("/onboarding/{case_id}/status", response_model=CaseStatusResponse,
         tags=["onboarding"])
def get_case_status(case_id: str) -> CaseStatusResponse:
    """Per-document status, extracted fields, mismatches, overall readiness."""
    case = _pipeline().get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return CaseStatusResponse(
        id=case.id,
        client_name=case.client_name,
        client_type=case.client_type,
        status=case.status,
        decision=case.decision.value if case.decision else None,
        requires_hitl=case.requires_hitl,
        ready=case.status == "ready",
        required_documents=case.required_documents,
        documents=case.documents,
        extracted_fields=case.extracted_fields,
        mismatches=case.mismatches,
        citations=case.citations,
    )


@app.post("/onboarding/{case_id}/documents", response_model=OnboardingCase,
          status_code=201, tags=["onboarding"])
def upload_document(case_id: str, data: DocumentInput) -> OnboardingCase:
    """Upload a document for a case and run the full check pipeline."""
    try:
        return _pipeline().upload_document(case_id, data)
    except KeyError:
        raise HTTPException(status_code=404, detail="Case not found") from None


@app.post("/onboarding/{case_id}/review/{action}",
          response_model=OnboardingCase, tags=["onboarding"])
def review_case(case_id: str, action: str, review: ReviewRequest) -> OnboardingCase:
    """Human-in-the-loop review: approve / reject / request_info.

    Only ``pending_review`` cases can be reviewed. This is the ONLY way
    a case reaches ``rejected`` status — the agents can propose a
    rejection, but a human must confirm it.
    """
    if action not in ("approve", "reject", "request_info"):
        raise HTTPException(
            status_code=400,
            detail="action must be approve, reject or request_info",
        )
    try:
        case = _pipeline().review_case(
            case_id,
            decision=action,
            reviewer=review.reviewer,
            note=review.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.get("/metrics", response_model=MetricsResponse, tags=["onboarding"])
def get_metrics() -> MetricsResponse:
    return MetricsResponse(**_pipeline().metrics())


@app.get("/audit", tags=["onboarding"])
def get_audit(case_id: str | None = Query(default=None)) -> JSONResponse:
    """Audit log — every decision and human review, with actor."""
    return JSONResponse(content=_pipeline().store.audit_entries(case_id))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app", host=settings.host, port=settings.port, reload=False
    )

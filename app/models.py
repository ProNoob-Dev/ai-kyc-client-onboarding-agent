"""Pydantic models for requests, responses and stored onboarding cases."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ClientType(str, Enum):
    INDIVIDUAL = "individual"
    COMPANY = "company"


class DocumentType(str, Enum):
    ID_PROOF = "id_proof"
    INCORPORATION = "incorporation"
    ADDRESS_PROOF = "address_proof"
    UNKNOWN = "unknown"


class DocumentStatus(str, Enum):
    OK = "ok"
    MISMATCH = "mismatch"
    UNCLEAR = "unclear"


class CaseStatus(str, Enum):
    SUBMITTED = "submitted"
    READY = "ready"  # all checks passed — clean pass-through
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    INFO_REQUESTED = "info_requested"


class ProposedDecision(str, Enum):
    PASS_THROUGH = "pass_through"
    NEEDS_REVIEW = "needs_review"
    PROPOSE_REJECT = "propose_reject"


class MismatchSeverity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"


# Required document types per client type (the onboarding requirements).
REQUIRED_DOCUMENTS: dict[str, list[str]] = {
    ClientType.INDIVIDUAL.value: [
        DocumentType.ID_PROOF.value,
        DocumentType.ADDRESS_PROOF.value,
    ],
    ClientType.COMPANY.value: [
        DocumentType.INCORPORATION.value,
        DocumentType.ID_PROOF.value,  # director / authorised signatory
        DocumentType.ADDRESS_PROOF.value,
    ],
}


class DocumentInput(BaseModel):
    """One onboarding document.

    ``content`` is the extracted text of the document. In production this
    comes from Azure Document Intelligence (Aadhaar, PAN, incorporation
    certificates, utility bills); the API accepts pre-extracted text so
    the service is demoable offline.
    """

    name: str = Field(..., examples=["aadhaar_card.pdf"])
    content: str = Field(default="", description="Extracted text of the document")
    content_type: str = Field(default="text/plain")
    doc_type_hint: str | None = Field(
        default=None,
        description="Optional hint: id_proof / incorporation / address_proof",
    )


class CaseCreate(BaseModel):
    client_name: str
    client_type: str = Field(
        ..., pattern="^(individual|company)$",
        description="individual or company",
    )
    country: str = "India"
    contact_email: str | None = None
    contact_phone: str | None = None
    # Optional structured facts the client declared on the application
    # form; extracted document fields are validated against these.
    declared_registration_number: str | None = None
    declared_address: str | None = None


class ReviewRequest(BaseModel):
    """Human-in-the-loop review. Only valid on a pending_review case."""

    reviewer: str
    note: str = ""


class Citation(BaseModel):
    """A KYC requirement rule the pipeline cited for its decision."""

    rule_id: str
    rule_title: str
    score: float
    excerpt: str


class AgentStep(BaseModel):
    agent: str
    action: str
    detail: str
    at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class AuditEntry(BaseModel):
    at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    actor: str  # "system" (an agent) or a reviewer name
    action: str
    detail: str


class Mismatch(BaseModel):
    """One field-level discrepancy, surfaced to the review queue."""

    id: str
    field: str  # e.g. legal_name, registration_number, address, document_expiry
    document_id: str | None = None
    expected: str | None = None
    found: str | None = None
    severity: str = MismatchSeverity.MEDIUM.value
    detail: str
    status: str = "open"  # open | accepted | resolved


class CaseDocument(BaseModel):
    id: str
    name: str
    content: str = ""
    content_type: str = "text/plain"
    doc_type: str = DocumentType.UNKNOWN.value
    doc_type_hint: str | None = None
    doc_type_confidence: float = 0.0
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    status: str = DocumentStatus.UNCLEAR.value
    mismatches: list[Mismatch] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class OnboardingCase(BaseModel):
    id: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    client_name: str
    client_type: str
    country: str = "India"
    declared_registration_number: str | None = None
    declared_address: str | None = None
    required_documents: list[str] = Field(default_factory=list)
    documents: list[CaseDocument] = Field(default_factory=list)
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    mismatches: list[Mismatch] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    decision: ProposedDecision | None = None
    proposed_decision: ProposedDecision | None = None
    requires_hitl: bool = False
    status: str = CaseStatus.SUBMITTED.value
    reviewer: str | None = None
    review_note: str | None = None
    processing_duration_ms: int = 0
    trace: list[AgentStep] = Field(default_factory=list)
    audit: list[AuditEntry] = Field(default_factory=list)
    pii_detected: list[str] = Field(default_factory=list)


class CaseSummary(BaseModel):
    """List-view shape — no free text, no PII."""

    id: str
    client_name: str
    client_type: str
    status: str
    decision: str | None
    documents_count: int
    mismatches_count: int
    requires_hitl: bool
    created_at: str


class CaseStatusResponse(BaseModel):
    """Per-case readiness view (GET /onboarding/{id}/status)."""

    id: str
    client_name: str
    client_type: str
    status: str
    decision: str | None
    requires_hitl: bool
    ready: bool
    required_documents: list[str]
    documents: list[CaseDocument]
    extracted_fields: dict[str, Any]
    mismatches: list[Mismatch]
    citations: list[Citation]


class MetricsResponse(BaseModel):
    total_cases: int
    ready: int
    pending_review: int
    approved_by_reviewer: int
    rejected_by_reviewer: int
    info_requested: int
    pass_through_rate: float
    avg_processing_ms: float
    mismatch_rate: float

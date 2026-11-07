"""Onboarding pipeline orchestration + human review.

The pipeline is the only entry point for creating cases, uploading
documents and reviewing them. It wires the LLM provider, the
requirements knowledge base and the LangGraph workflow together, and
enforces the one rule that matters most:

**No onboarding case is ever finally rejected without a human reviewer.**
The supervisor may only *propose* a rejection; the final rejection
happens in :func:`review_case`, by a named reviewer, and is written to
the audit log.
"""

from __future__ import annotations

import logging

from app.agents import OnboardingWorkflow
from app.config import settings
from app.llm import get_llm_provider
from app.models import (
    REQUIRED_DOCUMENTS,
    CaseCreate,
    CaseDocument,
    CaseStatus,
    DocumentInput,
    OnboardingCase,
)
from app.pg_store import build_case_store
from app.requirements_kb import build_requirements_kb

logger = logging.getLogger(__name__)


class OnboardingPipeline:
    def __init__(self, store=None) -> None:
        self.store = store or build_case_store(
            settings.database_url,
            settings.cases_db_file,
            settings.audit_log_file,
        )
        self.llm = get_llm_provider()
        self.requirements_kb = build_requirements_kb()
        self.workflow = OnboardingWorkflow(
            self.llm, self.requirements_kb, self.store
        )

    # -- create -------------------------------------------------------------
    def create_case(self, data: CaseCreate) -> OnboardingCase:
        case = OnboardingCase(
            id=self.store.new_id(),
            client_name=data.client_name,
            client_type=data.client_type,
            country=data.country,
            declared_registration_number=data.declared_registration_number,
            declared_address=data.declared_address,
            required_documents=list(
                REQUIRED_DOCUMENTS.get(data.client_type, [])
            ),
        )
        self.store.create(case)
        self.store.audit(
            case, actor="system", action="case-created",
            detail=(
                f"Onboarding case {case.id} created for {data.client_type} "
                f"client in {data.country}; required documents: "
                f"{', '.join(case.required_documents)}."
            ),
        )
        return case

    # -- upload -------------------------------------------------------------
    def upload_document(self, case_id: str, data: DocumentInput) -> OnboardingCase:
        case = self.store.get(case_id)
        if case is None:
            raise KeyError(case_id)
        doc = CaseDocument(
            id=self.store.new_doc_id(),
            name=data.name,
            content=data.content,
            content_type=data.content_type,
            doc_type_hint=data.doc_type_hint,
        )
        case.documents.append(doc)
        # The workflow only ever sees the PII-masked / tokenised version
        # of this payload (see agents._ingest / _validate); the raw
        # submission lives in the local case store only and is never
        # logged.
        self.store.audit(
            case, actor="system", action="document-uploaded",
            detail=f"Document {data.name} uploaded to case {case.id}.",
        )
        processed = self.workflow.run(case, doc)
        self.store.update(processed)
        return processed

    # -- review (HITL) --------------------------------------------------------
    def review_case(
        self, case_id: str, *, decision: str, reviewer: str, note: str = ""
    ) -> OnboardingCase | None:
        """Apply a human reviewer's decision.

        Only ``pending_review`` cases can be reviewed. A rejection here
        is the ONLY way a case reaches ``rejected`` status — the agents
        can never reject on their own.
        """
        case = self.store.get(case_id)
        if case is None:
            return None
        if case.status != CaseStatus.PENDING_REVIEW.value:
            raise ValueError(
                f"Case {case_id} is {case.status} and cannot be reviewed "
                "(only pending_review cases can be reviewed)."
            )
        mapping = {
            "approve": CaseStatus.APPROVED.value,
            "reject": CaseStatus.REJECTED.value,
            "request_info": CaseStatus.INFO_REQUESTED.value,
        }
        case.status = mapping[decision]
        case.reviewer = reviewer
        case.review_note = note
        if decision == "approve":
            for mismatch in case.mismatches:
                mismatch.status = "accepted"
        self.store.audit(
            case,
            actor=f"reviewer:{reviewer}",
            action=f"review-{decision}",
            detail=(
                f"Human review: {decision}. "
                f"Proposed decision was {case.proposed_decision}. "
                f"Note: {note or '-'}"
            ),
        )
        self.store.update(case)
        return case

    # -- reads ------------------------------------------------------------------
    def get_case(self, case_id: str) -> OnboardingCase | None:
        return self.store.get(case_id)

    def list_cases(
        self,
        *,
        status: str | None = None,
        client_type: str | None = None,
    ) -> list[OnboardingCase]:
        cases = self.store.list_all()
        if status:
            cases = [c for c in cases if c.status == status]
        if client_type:
            cases = [c for c in cases if c.client_type == client_type]
        return cases

    def review_queue(self) -> list[OnboardingCase]:
        """Pending cases, oldest first — the human review queue."""
        pending = [
            c for c in self.store.list_all()
            if c.status == CaseStatus.PENDING_REVIEW.value
        ]
        return sorted(pending, key=lambda c: c.created_at)

    def metrics(self) -> dict:
        cases = self.store.list_all()
        total = len(cases)
        ready = sum(1 for c in cases if c.status == "ready")
        pending = sum(1 for c in cases if c.status == "pending_review")
        approved = sum(1 for c in cases if c.status == "approved")
        rejected = sum(1 for c in cases if c.status == "rejected")
        info = sum(1 for c in cases if c.status == "info_requested")
        passed = ready + approved
        mismatched = sum(1 for c in cases if c.mismatches)
        return {
            "total_cases": total,
            "ready": ready,
            "pending_review": pending,
            "approved_by_reviewer": approved,
            "rejected_by_reviewer": rejected,
            "info_requested": info,
            "pass_through_rate": round(passed / total, 3) if total else 0.0,
            "avg_processing_ms": round(
                sum(c.processing_duration_ms for c in cases) / total, 1
            ) if total else 0.0,
            "mismatch_rate": round(mismatched / total, 3) if total else 0.0,
        }

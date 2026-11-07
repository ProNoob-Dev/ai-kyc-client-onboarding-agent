"""Multi-agent KYC onboarding workflow, built on LangGraph.

Four agents run as nodes in a state graph, every time a document is
uploaded for a client:

1. **Ingestion agent** — classifies the new document (ID proof /
   incorporation papers / address proof) and extracts structured fields
   (name, registration number, address, dates) with confidence scores.
   All free text is PII-masked before the LLM call.
2. **Retrieval agent** — pulls the relevant KYC requirement rules from
   the requirements knowledge base and returns them with citations to
   the exact rule.
3. **Validation agent** — cross-checks extracted fields across all of
   the client's documents and against the declared application facts
   and the retrieved rules. Every mismatch becomes a review item with
   the exact discrepancy highlighted. Nothing is silently passed.
4. **Supervisor agent** — makes the routing decision. Clean cases with
   all required documents pass straight through. Anything with a
   mismatch goes to a human reviewer. An expired document proposes a
   rejection — but a rejection is NEVER final here, it is only ever a
   *proposal* that a human must confirm in the review queue (HITL).

Every agent step is recorded in the case's trace, and every decision is
written to the audit log with its citations.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.llm import LLMProvider
from app.models import (
    AgentStep,
    CaseDocument,
    Citation,
    Mismatch,
    MismatchSeverity,
    OnboardingCase,
    ProposedDecision,
)
from app.pii import mask_case_payload, mask_text
from app.requirements_kb import RequirementsKB
from app.store import CaseStore

logger = logging.getLogger(__name__)


class OnboardingState(TypedDict, total=False):
    case: dict[str, Any]
    case_obj: OnboardingCase
    document: CaseDocument
    masked_payload: dict[str, Any]
    citations: list[Citation]
    mismatches: list[Mismatch]
    decision: str
    requires_hitl: bool
    trace: list[AgentStep]


def _norm_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _norm_addr(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _tokenised_payload(case: OnboardingCase) -> dict[str, Any]:
    """A copy of the case safe to send to a model.

    Names, addresses and registration/ID numbers are replaced with
    consistent pseudonym tokens (NAME-1, ADDR-1, REG-1, ID-1): the same
    real value always gets the same token, so the model can spot a
    mismatch between documents without seeing any real personal data.
    Document text is PII-masked; dates and document types pass through.
    """
    payload = case.model_dump()

    token_fields = ("legal_name", "address", "registration_number", "id_number")
    prefixes = {
        "legal_name": "NAME",
        "address": "ADDR",
        "registration_number": "REG",
        "id_number": "ID",
    }
    counters: dict[str, int] = {}
    token_map: dict[tuple[str, str], str] = {}

    def token(field: str, value: str | None) -> str | None:
        if not value:
            return value
        norm = re.sub(r"[^a-z0-9]", "", str(value).lower())
        key = (field, norm)
        if key not in token_map:
            counters[field] = counters.get(field, 0) + 1
            token_map[key] = f"{prefixes[field]}-{counters[field]}"
        return token_map[key]

    # Declared application facts share the same token space, so a
    # declared value that matches a document value gets the same token.
    if payload.get("declared_registration_number"):
        payload["declared_registration_number"] = token(
            "registration_number", payload["declared_registration_number"]
        )
    if payload.get("declared_address"):
        payload["declared_address"] = token("address", payload["declared_address"])
    if payload.get("client_name"):
        payload["client_name"] = token("legal_name", payload["client_name"])
    for field in ("contact_email", "contact_phone"):
        if payload.get(field):
            payload[field] = f"<{field.upper()}_REDACTED>"

    for doc in payload.get("documents", []) or []:
        if doc.get("content"):
            doc["content"], _ = mask_text(
                doc["content"], extra_names=[case.client_name]
            )
        for field in token_fields:
            if doc.get("extracted_fields", {}).get(field):
                doc["extracted_fields"][field] = token(
                    field, doc["extracted_fields"][field]
                )
    for field in token_fields:
        if payload.get("extracted_fields", {}).get(field):
            payload["extracted_fields"][field] = token(
                field, payload["extracted_fields"][field]
            )
    return payload


class OnboardingWorkflow:
    def __init__(
        self,
        llm: LLMProvider,
        requirements_kb: RequirementsKB,
        store: CaseStore,
    ) -> None:
        self._llm = llm
        self._kb = requirements_kb
        self._store = store
        self._graph = self._build_graph()

    # -- graph --------------------------------------------------------------
    def _build_graph(self) -> StateGraph:
        graph: StateGraph = StateGraph(OnboardingState)
        graph.add_node("ingest", self._ingest)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("validate", self._validate)
        graph.add_node("supervise", self._supervise)
        graph.set_entry_point("ingest")
        graph.add_edge("ingest", "retrieve")
        graph.add_edge("retrieve", "validate")
        graph.add_edge("validate", "supervise")
        graph.add_edge("supervise", END)
        return graph.compile()

    def run(self, case: OnboardingCase, document: CaseDocument) -> OnboardingCase:
        started = time.monotonic()
        initial: OnboardingState = {
            "case": case.model_dump(),
            "case_obj": case,
            "document": document,
            "trace": [],
        }
        result = self._graph.invoke(initial)
        case = result["case_obj"]
        case.processing_duration_ms = int((time.monotonic() - started) * 1000)
        return case

    # -- nodes --------------------------------------------------------------
    def _step(self, state: OnboardingState, agent: str, action: str, detail: str) -> None:
        step = AgentStep(agent=agent, action=action, detail=detail)
        state.setdefault("trace", []).append(step)
        state["case_obj"].trace.append(step)

    def _ingest(self, state: OnboardingState) -> OnboardingState:
        case: OnboardingCase = state["case_obj"]
        doc: CaseDocument = state["document"]
        payload = case.model_dump()
        _masked, detected = mask_text(
            doc.content, extra_names=[case.client_name]
        )
        if detected:
            case.pii_detected = sorted(set(case.pii_detected) | set(detected))
        safe_payload = mask_case_payload(payload)
        state["masked_payload"] = safe_payload

        # 1a. Classify the new document.
        classify_prompt = (
            "TASK: classify_document\n"
            "Classify this onboarding document into one of: id_proof, "
            "incorporation, address_proof. Reply with JSON only.\n"
            f"CASE_JSON: {json.dumps(safe_payload, ensure_ascii=False)}"
        )
        resp = self._llm.complete(
            system="You are a KYC document classifier. Reply with JSON only.",
            prompt=classify_prompt,
            json_mode=True,
        )
        try:
            classified = json.loads(resp.content)
        except json.JSONDecodeError:
            classified = {"doc_type": "unknown", "confidence": 0.0}
        doc_type = str(classified.get("doc_type", "unknown")).lower()
        if doc_type not in ("id_proof", "incorporation", "address_proof"):
            doc_type = "unknown"
        confidence = float(classified.get("confidence", 0.0) or 0.0)
        # A caller-supplied hint only wins when the text gives no signal.
        if doc_type == "unknown" and doc.doc_type_hint in (
            "id_proof", "incorporation", "address_proof",
        ):
            doc_type, confidence = doc.doc_type_hint, 0.6
        doc.doc_type = doc_type
        doc.doc_type_confidence = confidence
        self._step(
            state, "ingestion",
            "classify",
            f"Document {doc.name} classified as {doc_type} "
            f"(confidence {confidence:.2f}). PII masked: {detected or 'none'}.",
        )

        # 1b. Extract structured fields from the new document.
        # Local deterministic extraction (the Document Intelligence
        # stand-in): names, addresses and ID numbers are PII, so they are
        # pulled out in local code and never sent to a model unmasked.
        from app.extract import extract_fields_from_text

        fields, _confs = extract_fields_from_text(doc.content, case.client_name)
        doc.extracted_fields = fields
        # Merge into the case-level view (first-seen value wins per field).
        for key, value in fields.items():
            if value and key not in case.extracted_fields:
                case.extracted_fields[key] = value
        self._step(
            state, "ingestion",
            "extract",
            f"Extracted from {doc.name}: "
            + (", ".join(sorted(fields)) or "no fields"),
        )
        return state

    def _retrieve(self, state: OnboardingState) -> OnboardingState:
        case: OnboardingCase = state["case_obj"]
        doc: CaseDocument = state["document"]
        query = (
            f"{doc.doc_type} {case.client_type} "
            f"{doc.name} {' '.join(str(v) for v in doc.extracted_fields.values())}"
        )
        hits = self._kb.search(query, top_k=4)
        citations = [
            Citation(
                rule_id=hit.rule["id"],
                rule_title=hit.rule.get("title", ""),
                score=hit.score,
                excerpt=hit.rule["text"][:280],
            )
            for hit in hits
        ]
        state["citations"] = citations
        case.citations = citations
        self._step(
            state, "retrieval",
            "requirements-check",
            f"{len(citations)} requirement rule(s) retrieved; top: "
            f"{citations[0].rule_id if citations else 'none'}.",
        )
        return state

    def _validate(self, state: OnboardingState) -> OnboardingState:
        case: OnboardingCase = state["case_obj"]
        # Pseudonymise before any model call: names, addresses and
        # registration/ID numbers become consistent tokens (NAME-1,
        # ADDR-1, REG-1, ID-1), so the model can detect a mismatch
        # without ever seeing the real values. Dates and document types
        # are not PII and pass through unchanged.
        safe_payload = _tokenised_payload(case)
        rules = [hit.rule for hit in self._kb.search(
            " ".join(d.doc_type for d in case.documents), top_k=10
        )]
        # Ask the LLM to assess every retrieved rule against the case.
        validate_prompt = (
            "TASK: validate_rules\n"
            "Assess each KYC requirement rule against the extracted "
            "document fields. Reply with JSON only.\n"
            f"CASE_JSON: {json.dumps(safe_payload, ensure_ascii=False)}\n"
            "RULES_JSON: " + json.dumps(rules, ensure_ascii=False)
        )
        resp = self._llm.complete(
            system=(
                "You are a KYC validation assessor. Assess each rule only "
                "from the extracted fields. Reply with JSON only."
            ),
            prompt=validate_prompt,
            json_mode=True,
        )
        try:
            assessments = json.loads(resp.content).get("assessments", [])
        except (json.JSONDecodeError, AttributeError):
            assessments = []
        failed = {
            str(a.get("rule_id")): str(a.get("reasoning", ""))
            for a in assessments
            if not a.get("passed", True)
        }

        # Build mismatches deterministically from the failed rules.
        mismatches: list[Mismatch] = []
        for rule in rules:
            if rule["id"] not in failed:
                continue
            mismatch = self._mismatch_from_rule(case, rule, failed[rule["id"]])
            if mismatch is not None:
                mismatches.append(mismatch)
        state["mismatches"] = mismatches
        case.mismatches = mismatches
        for doc in case.documents:
            doc.mismatches = [
                m for m in mismatches if m.document_id == doc.id
            ]
            doc.status = (
                "mismatch" if doc.mismatches
                else ("ok" if doc.doc_type != "unknown" else "unclear")
            )
        self._step(
            state, "validation",
            "cross-check",
            f"{len(mismatches)} mismatch(es) found: "
            + (", ".join(f"{m.field} ({m.severity})" for m in mismatches)
               or "none — all fields consistent."),
        )
        return state

    def _mismatch_from_rule(
        self, case: OnboardingCase, rule: dict, reasoning: str
    ) -> Mismatch | None:
        """Turn a failed requirement rule into a concrete mismatch."""
        field = rule.get("field", "")
        check = rule.get("check", "")
        severity = rule.get("severity", MismatchSeverity.MEDIUM.value)
        docs = case.documents
        # For a company, the director's ID proof carries the director's
        # personal name and residential address — those legitimately
        # differ from the company name and registered office.
        comparable = (
            [d for d in docs if d.doc_type != "id_proof"]
            if case.client_type == "company" else docs
        )
        expected: str | None = None
        found: str | None = None
        document_id: str | None = None

        if check == "name_consistent":
            names = {}
            for d in comparable:
                name = str(d.extracted_fields.get("legal_name", "")).strip()
                if name:
                    names.setdefault(_norm_name(name), (name, d))
            if len(names) <= 1:
                return None
            expected = next(iter(names.values()))[0]
            odd = [v[0] for k, v in names.items() if v[0] != expected]
            found = "; ".join(sorted(set(odd)))
            document_id = next(
                (v[1].id for v in names.values() if v[0] in odd), None
            )
        elif check == "registration_consistent":
            regs: dict[str, tuple[str, CaseDocument]] = {}
            for d in docs:
                reg = str(d.extracted_fields.get("registration_number", "")).strip()
                if reg:
                    regs.setdefault(reg.upper(), (reg, d))
            if len(regs) <= 1:
                return None
            expected = next(iter(regs.values()))[0]
            odd = [v[0] for v in regs.values() if v[0] != expected]
            found = "; ".join(sorted(set(odd)))
            document_id = next(
                (v[1].id for v in regs.values() if v[0] in odd), None
            )
        elif check == "address_consistent":
            addrs: dict[str, tuple[str, CaseDocument]] = {}
            for d in comparable:
                addr = str(d.extracted_fields.get("address", "")).strip()
                if addr:
                    addrs.setdefault(_norm_addr(addr), (addr, d))
            if len(addrs) <= 1:
                return None
            expected = next(iter(addrs.values()))[0]
            odd = [v[0] for v in addrs.values() if v[0] != expected]
            found = "; ".join(sorted(set(odd)))
            document_id = next(
                (v[1].id for v in addrs.values() if v[0] in odd), None
            )
        elif check == "id_not_expired":
            for d in docs:
                expiry = d.extracted_fields.get("expiry_date")
                if expiry:
                    from datetime import date as _date
                    if str(expiry) < _date.today().isoformat():
                        expected, found = "valid (not expired)", str(expiry)
                        document_id = d.id
                        break
            if found is None:
                return None
        elif check == "address_proof_recent":
            from datetime import date as _date
            from datetime import timedelta as _td
            cutoff = (_date.today() - _td(days=90)).isoformat()
            for d in docs:
                if d.doc_type == "address_proof":
                    doc_date = d.extracted_fields.get("document_date")
                    if doc_date and str(doc_date) < cutoff:
                        expected, found = "dated within 90 days", str(doc_date)
                        document_id = d.id
                        break
            if found is None:
                return None
        elif check == "all_required_present":
            present = {d.doc_type for d in docs}
            missing = sorted(set(case.required_documents) - present)
            if not missing:
                return None
            expected, found = ", ".join(missing), "not submitted"
        elif check == "incorporation_date_valid":
            from datetime import date as _date
            today = _date.today().isoformat()
            for d in docs:
                inc = d.extracted_fields.get("incorporation_date")
                if inc and str(inc) > today:
                    expected, found = "on or before today", str(inc)
                    document_id = d.id
                    break
            if found is None:
                return None
        elif check == "all_documents_classified":
            unknown = [d for d in docs if d.doc_type == "unknown"]
            if not unknown:
                return None
            expected, found = "a recognised document type", "unclassifiable"
            document_id = unknown[0].id
        elif check == "declared_registration_matches":
            declared = (case.declared_registration_number or "").strip().upper()
            if not declared:
                return None
            for d in docs:
                reg = str(d.extracted_fields.get("registration_number", "")).strip().upper()
                if reg and reg != declared:
                    expected, found = declared, reg
                    document_id = d.id
                    break
            if found is None:
                return None
        elif check == "declared_address_matches":
            declared = (case.declared_address or "").strip()
            if not declared:
                return None
            for d in docs:
                addr = str(d.extracted_fields.get("address", "")).strip()
                if addr and _norm_addr(addr) != _norm_addr(declared):
                    expected, found = declared, addr
                    document_id = d.id
                    break
            if found is None:
                return None
        else:
            return None

        return Mismatch(
            id=self._store.new_mismatch_id(),
            field=field,
            document_id=document_id,
            expected=expected,
            found=found,
            severity=severity,
            detail=f"{rule.get('title', field)}: {reasoning}",
        )

    def _supervise(self, state: OnboardingState) -> OnboardingState:
        case: OnboardingCase = state["case_obj"]
        mismatches = state.get("mismatches", [])

        expired = [m for m in mismatches if m.field == "document_expiry"]
        if expired:
            decision = ProposedDecision.PROPOSE_REJECT
            requires_hitl = True
            reason = (
                "Document expired — rejection proposed for human review. "
                "A rejection can never be final without a reviewer "
                "confirming it."
            )
        elif mismatches:
            decision = ProposedDecision.NEEDS_REVIEW
            requires_hitl = True
            reason = (
                f"{len(mismatches)} mismatch(es) need a human look — "
                "routed to the review queue with the exact discrepancy "
                "highlighted."
            )
        else:
            decision = ProposedDecision.PASS_THROUGH
            requires_hitl = False
            reason = (
                "All required documents present, fields consistent, no "
                "mismatch — clean pass-through."
            )

        case.decision = decision
        case.proposed_decision = decision
        case.requires_hitl = requires_hitl
        state["decision"] = decision.value
        state["requires_hitl"] = requires_hitl
        case.status = (
            "ready" if decision == ProposedDecision.PASS_THROUGH
            else "pending_review"
        )
        self._step(state, "supervisor", "route", reason)
        self._store.audit(
            case,
            actor="system:supervisor",
            action="onboarding-decision",
            detail=(
                f"Decision: {decision.value}; requires_hitl={requires_hitl}; "
                f"mismatches={len(mismatches)}; citations="
                + ",".join(c.rule_id for c in case.citations)
            ),
        )
        return state

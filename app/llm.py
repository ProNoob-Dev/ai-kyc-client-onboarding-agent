"""LLM provider interface.

The pipeline NEVER calls a model directly — it always goes through
``LLMProvider.complete()``. Two implementations ship:

* :class:`MockLLMProvider` (default) — deterministic, offline, rule-based.
  It implements the same prompts and the same JSON contracts as the real
  provider, so the whole system runs, demos and tests without any API key.
* :class:`AzureOpenAIProvider` — real Azure OpenAI (GPT-4o), activated by
  setting ``LLM_PROVIDER=azure`` plus ``AZURE_OPENAI_ENDPOINT``,
  ``AZURE_OPENAI_API_KEY`` and ``AZURE_OPENAI_DEPLOYMENT``.

No API key is ever hardcoded. If ``LLM_PROVIDER=azure`` is requested but
the credentials are missing, startup falls back to the mock provider with
a loud warning instead of crashing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMProvider(Protocol):
    def complete(
        self, *, system: str, prompt: str, json_mode: bool = False
    ) -> LLMResponse:
        ...


# ---------------------------------------------------------------------------
# Mock provider (default)
# ---------------------------------------------------------------------------

_DOC_TYPE_KEYWORDS: dict[str, list[str]] = {
    "id_proof": [
        "aadhaar", "aadhar", "pan card", "passport", "voter", "identity card",
        "driving licence", "driving license", "date of birth", "dob",
        "uidai", "permanent account number",
    ],
    "incorporation": [
        "certificate of incorporation", "incorporation", "memorandum of association",
        "articles of association", "cin", "company identification",
        "registered office", "registrar of companies", "roc",
        "private limited", "llp", "limited liability partnership",
    ],
    "address_proof": [
        "utility bill", "electricity bill", "water bill", "gas bill",
        "bank statement", "telephone bill", "rent agreement",
        "address proof", "proof of address", "residential address",
    ],
}

# Phrases that signal a field value in document text, e.g.
# "Name: Aarav Sharma" / "Registration No.: U74900MH2019PTC123456".
# (Extraction itself lives in app/extract.py — a deterministic local
# step, standing in for Azure Document Intelligence. The LLM never
# extracts PII fields from raw text.)


class MockLLMProvider:
    """Deterministic stand-in for Azure OpenAI.

    The mock reads the structured case JSON embedded in the prompt and
    applies the same decision rules a prompted model would be instructed
    to follow. It is deliberately boring: no randomness, so evals are
    reproducible.
    """

    model = "mock-llm-v1"

    def complete(
        self, *, system: str, prompt: str, json_mode: bool = False
    ) -> LLMResponse:
        task = self._task_of(prompt)
        case = self._case_of(prompt)
        rules = self._rules_of(prompt)

        if task == "classify_document":
            result = self._classify_document(case)
        elif task == "validate_rules":
            result = self._validate_rules(case, rules)
        else:  # generic fallback — echo a safe structured answer
            result = {"answer": "not-enough-information"}

        content = json.dumps(result)
        return LLMResponse(
            content=content,
            model=self.model,
            prompt_tokens=len(prompt.split()),
            completion_tokens=len(content.split()),
        )

    # -- prompt parsing ---------------------------------------------------
    @staticmethod
    def _task_of(prompt: str) -> str:
        match = re.search(r"TASK:\s*(\w+)", prompt)
        return match.group(1).lower() if match else ""

    @staticmethod
    def _case_of(prompt: str) -> dict[str, Any]:
        match = re.search(r"CASE_JSON:\s*(\{.*?\})\s*(?:\n|$)", prompt, re.DOTALL)
        if not match:
            return {}
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _rules_of(prompt: str) -> list[dict[str, Any]]:
        match = re.search(r"RULES_JSON:\s*(\[.*?\])\s*(?:\n|$)", prompt, re.DOTALL)
        if not match:
            return []
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return []

    # -- tasks ------------------------------------------------------------
    @staticmethod
    def _classify_document(case: dict[str, Any]) -> dict[str, Any]:
        docs = case.get("documents", []) or []
        doc = docs[-1] if docs else {}
        text = (
            str(doc.get("name", ""))
            + " "
            + str(doc.get("content", ""))
        ).lower().replace("_", " ").replace("-", " ")
        hint = str(doc.get("doc_type_hint") or "").lower()

        scores = {
            dtype: sum(1 for kw in kws if kw in text)
            for dtype, kws in _DOC_TYPE_KEYWORDS.items()
        }
        best = max(scores, key=lambda k: scores[k])
        confidence = 0.9 if scores[best] >= 2 else (0.7 if scores[best] == 1 else 0.4)
        doc_type = best if scores[best] > 0 else "unknown"
        # A caller-supplied hint only wins when the text gives no signal.
        if doc_type == "unknown" and hint in _DOC_TYPE_KEYWORDS:
            doc_type, confidence = hint, 0.6
        return {
            "doc_type": doc_type,
            "confidence": confidence,
            "reasoning": f"Matched {scores[best]} keyword(s) for {best}.",
        }

    @staticmethod
    def _validate_rules(
        case: dict[str, Any], rules: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Assess each retrieved KYC rule against the extracted fields.

        The mock checks the rule's ``check`` keyword against the case
        payload: name/registration/address consistency, document expiry,
        document recency and missing documents. A rule passes only when
        its check holds on the extracted data.
        """
        extracted = case.get("extracted_fields", {}) or {}
        docs = case.get("documents", []) or []
        assessments: list[dict[str, Any]] = []

        for rule in rules:
            check = str(rule.get("check", ""))
            passed, reason = MockLLMProvider._check_rule(check, case, extracted, docs)
            assessments.append({
                "rule_id": rule.get("id"),
                "passed": passed,
                "reasoning": reason,
            })
        return {"assessments": assessments}

    @staticmethod
    def _check_rule(
        check: str, case: dict[str, Any], extracted: dict[str, Any],
        docs: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        # For a company, the director's ID proof carries the director's
        # personal name and residential address — those legitimately
        # differ from the company name and registered office. Name and
        # address consistency are therefore checked across the company
        # documents only (incorporation + address proof).
        def _comparable(field_docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if str(case.get("client_type", "")).lower() == "company":
                return [d for d in field_docs if d.get("doc_type") != "id_proof"]
            return field_docs

        if check == "name_consistent":
            names = {
                str(d.get("extracted_fields", {}).get("legal_name", "")).strip().lower()
                for d in _comparable(docs)
                if d.get("extracted_fields", {}).get("legal_name")
            }
            if len(names) <= 1:
                return True, "Name is consistent across documents."
            return False, f"Name differs across documents: {sorted(names)}."
        if check == "registration_consistent":
            regs = {
                str(d.get("extracted_fields", {}).get("registration_number", "")).strip().upper()
                for d in docs
                if d.get("extracted_fields", {}).get("registration_number")
            }
            if len(regs) <= 1:
                return True, "Registration number is consistent across documents."
            return False, f"Registration number differs across documents: {sorted(regs)}."
        if check == "address_consistent":
            addrs = {
                re.sub(r"[^a-z0-9]", "", str(d.get("extracted_fields", {}).get("address", "")).lower())
                for d in _comparable(docs)
                if d.get("extracted_fields", {}).get("address")
            }
            if len(addrs) <= 1:
                return True, "Address is consistent across documents."
            return False, "Address differs across documents."
        if check == "id_not_expired":
            today = date.today().isoformat()
            for d in docs:
                expiry = d.get("extracted_fields", {}).get("expiry_date")
                if expiry and str(expiry) < today:
                    return False, f"ID document expired on {expiry}."
            return True, "No expired ID document found."
        if check == "address_proof_recent":
            cutoff = (date.today() - timedelta(days=90)).isoformat()
            for d in docs:
                if d.get("doc_type") == "address_proof":
                    doc_date = d.get("extracted_fields", {}).get("document_date")
                    if doc_date and str(doc_date) < cutoff:
                        return False, f"Address proof dated {doc_date} is older than 90 days."
            return True, "Address proof is within 90 days."
        if check == "all_required_present":
            required = set(case.get("required_documents", []) or [])
            present = {str(d.get("doc_type")) for d in docs}
            missing = sorted(required - present)
            if missing:
                return False, f"Missing required document(s): {', '.join(missing)}."
            return True, "All required documents present."
        if check == "incorporation_date_valid":
            today = date.today().isoformat()
            for d in docs:
                inc = d.get("extracted_fields", {}).get("incorporation_date")
                if inc and str(inc) > today:
                    return False, f"Incorporation date {inc} is in the future."
            return True, "Incorporation date is valid."
        if check == "all_documents_classified":
            unknown = [str(d.get("name", "?")) for d in docs
                       if d.get("doc_type") in ("unknown", "", None)]
            if unknown:
                return False, (
                    "Could not classify document(s): " + ", ".join(unknown)
                    + ". A human must identify them."
                )
            return True, "All documents classified."
        if check == "declared_registration_matches":
            declared = str(case.get("declared_registration_number", "")).strip()
            if not declared or declared.startswith("<"):
                return True, "No declared registration number to check against."
            for d in docs:
                reg = str(d.get("extracted_fields", {}).get("registration_number", "")).strip()
                if reg and reg.upper() != declared.upper():
                    return False, (
                        f"Registration number {reg} does not match the "
                        f"declared {declared}."
                    )
            return True, "Registration number matches the declaration."
        if check == "declared_address_matches":
            declared = str(case.get("declared_address", "")).strip()
            if not declared or declared.startswith("<"):
                return True, "No declared address to check against."
            for d in docs:
                addr = str(d.get("extracted_fields", {}).get("address", "")).strip()
                if addr and re.sub(r"[^a-z0-9]", "", addr.lower()) != re.sub(
                    r"[^a-z0-9]", "", declared.lower()
                ):
                    return False, "Address does not match the declared address."
            return True, "Address matches the declaration."
        return True, "No automated check for this rule; treated as passing."


# ---------------------------------------------------------------------------
# Azure OpenAI provider
# ---------------------------------------------------------------------------

class AzureOpenAIProvider:
    """Real Azure OpenAI provider (GPT-4o)."""

    def __init__(self, *, endpoint: str, api_key: str, deployment: str,
                 api_version: str) -> None:
        from openai import AzureOpenAI

        self._client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
        )
        self._deployment = deployment
        self.model = deployment

    def complete(
        self, *, system: str, prompt: str, json_mode: bool = False
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._deployment,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        usage = resp.usage
        return LLMResponse(
            content=choice.message.content or "",
            model=self._deployment,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )


def get_llm_provider() -> LLMProvider:
    """Build the configured provider, falling back to mock if needed."""
    from app.config import settings

    if settings.llm_provider == "azure":
        if settings.azure_openai_endpoint and settings.azure_openai_api_key:
            logger.info("LLM provider: Azure OpenAI (%s)",
                        settings.azure_openai_deployment)
            return AzureOpenAIProvider(
                endpoint=settings.azure_openai_endpoint,
                api_key=settings.azure_openai_api_key,
                deployment=settings.azure_openai_deployment,
                api_version=settings.azure_openai_api_version,
            )
        logger.warning(
            "LLM_PROVIDER=azure but Azure credentials are missing — "
            "falling back to the mock provider. Set AZURE_OPENAI_ENDPOINT "
            "and AZURE_OPENAI_API_KEY to use the real model."
        )
    logger.info("LLM provider: mock (offline, deterministic)")
    return MockLLMProvider()

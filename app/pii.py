"""PII masking.

Every piece of free text is masked BEFORE it is sent to any LLM call,
and the masked version is what lands in traces and logs. The original
text is kept only in the encrypted-at-rest case store (here: the local
JSON store) and is never logged.

KYC documents are PII-dense by definition — Aadhaar, PAN, passports,
company registration numbers — so the patterns here are deliberately
conservative: mask anything that looks like an identifier.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    # Indian mobile numbers, with or without country code / spaces / dashes.
    (
        "PHONE",
        re.compile(r"(?:\+91[\s\-]?)?(?:\b[6-9]\d{4}[\s\-]?\d{5}\b)"),
    ),
    # Aadhaar: 4-4-4 digits.
    ("AADHAAR", re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b")),
    # PAN: 5 letters, 4 digits, 1 letter.
    ("PAN", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")),
    # Indian passport: 1 letter + 7 digits.
    ("PASSPORT", re.compile(r"\b[A-Z][0-9]{7}\b")),
    # Company Identification Number (CIN): U + 5 digits + 2 letters +
    # 4 digits + 3 letters + 6 digits, e.g. U74900MH2019PTC123456.
    ("CIN", re.compile(r"\bU\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b")),
    # Bank account / long digit runs (10-18 digits) that are not dates.
    ("ACCOUNT", re.compile(r"\b\d{10,18}\b")),
]


def mask_text(text: str, extra_names: list[str] | None = None) -> tuple[str, list[str]]:
    """Return (masked_text, detected_pii_types).

    Names passed in ``extra_names`` (e.g. the client's name) are also
    masked, so a person's name never reaches the model in free text.
    """
    masked = text or ""
    detected: list[str] = []

    for label, pattern in _PATTERNS:
        if pattern.search(masked):
            masked = pattern.sub(f"<{label}_REDACTED>", masked)
            detected.append(label)

    for name in extra_names or []:
        name = (name or "").strip()
        if len(name) >= 3 and name.lower() in masked.lower():
            masked = re.sub(re.escape(name), "<NAME_REDACTED>", masked,
                            flags=re.IGNORECASE)
            if "NAME" not in detected:
                detected.append("NAME")

    return masked, detected


def mask_case_payload(payload: dict) -> dict:
    """Return a copy of a case payload safe to send to an LLM.

    Structured fields the pipeline needs (client type, dates, document
    type hints) are kept; free-text fields are masked.
    """
    safe = dict(payload)
    if "client_name" in safe:
        safe["client_name"] = "<NAME_REDACTED>"
    if "contact_email" in safe:
        safe["contact_email"] = "<EMAIL_REDACTED>"
    if "contact_phone" in safe:
        safe["contact_phone"] = "<PHONE_REDACTED>"
    if safe.get("declared_registration_number"):
        safe["declared_registration_number"] = "<REGISTRATION_REDACTED>"
    if safe.get("declared_address"):
        safe["declared_address"] = "<ADDRESS_REDACTED>"
    for field in ("notes",):
        if field in safe and isinstance(safe[field], str):
            safe[field], _ = mask_text(safe[field])
    if "documents" in safe and isinstance(safe["documents"], list):
        safe_docs = []
        for doc in safe["documents"]:
            doc = dict(doc)
            if "content" in doc and isinstance(doc["content"], str):
                doc["content"], _ = mask_text(
                    doc["content"], extra_names=[payload.get("client_name", "")]
                )
            safe_docs.append(doc)
        safe["documents"] = safe_docs
    return safe

"""Deterministic field extraction from onboarding document text.

This is the local stand-in for Azure Document Intelligence: in
production, Document Intelligence (an OCR/extraction service, not a
chat model) pulls structured fields out of the document before any LLM
sees the text. The extraction runs here, in local code, for the same
reason — names, addresses and ID numbers are PII, so they are pulled
out deterministically and never sent to a model unmasked.

The validation agent later replaces names/addresses/registration
numbers with consistent pseudonym tokens before any model call, so the
model can still detect a mismatch without ever seeing the real values.
"""

from __future__ import annotations

import re

# Phrases that signal a field value in document text, e.g.
# "Name: Aarav Sharma" / "Registration No.: U74900MH2019PTC123456".
_FIELD_PATTERNS: dict[str, list[str]] = {
    "legal_name": [
        r"(?:name|applicant|holder|company name|client name)\s*[:\-]\s*([^\n,;]+)",
    ],
    "registration_number": [
        r"(?:cin|registration no\.?|reg\.?\s*no\.?|company identification number)\s*[:\-]\s*([A-Z0-9\- ]{6,30})",
        r"\b(U\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\b",
    ],
    "id_number": [
        r"(?:aadhaar no\.?|aadhar no\.?|uid)\s*[:\-]\s*([\d\s\-]{12,17})",
        r"(?:pan)\s*[:\-]\s*([A-Z]{5}[0-9]{4}[A-Z])",
        r"(?:passport no\.?)\s*[:\-]\s*([A-Z][0-9]{7})",
    ],
    "address": [
        r"(?:address|registered office|residential address)\s*[:\-]\s*([^\n]+)",
    ],
    "date_of_birth": [
        r"(?:date of birth|dob)\s*[:\-]\s*(\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4})",
    ],
    "incorporation_date": [
        r"(?:date of incorporation|incorporated on|incorporation date)\s*[:\-]\s*(\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4})",
    ],
    "expiry_date": [
        r"(?:valid until|valid upto|expiry date|expires on|valid till)\s*[:\-]\s*(\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4})",
    ],
    "document_date": [
        r"(?:bill date|statement date|issue date|dated)\s*[:\-]\s*(\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4})",
    ],
}


def _norm_date(value: str | None) -> str | None:
    """Normalise DD/MM/YYYY or DD-MM-YYYY to ISO YYYY-MM-DD."""
    if not value:
        return None
    value = value.strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", value)
    if match:
        return value
    match = re.match(r"^(\d{2})[/-](\d{2})[/-](\d{4})$", value)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"
    return value


def extract_fields_from_text(
    text: str, client_name: str | None = None
) -> tuple[dict[str, str], dict[str, float]]:
    """Extract structured fields from one document's text.

    Returns (fields, confidences). Field values are returned as found —
    callers must not put them in logs or model prompts unmasked.
    """
    fields: dict[str, str] = {}
    confidences: dict[str, float] = {}

    for field, patterns in _FIELD_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                value = match.group(1).strip().strip(",;")
                if field in ("date_of_birth", "incorporation_date",
                             "expiry_date", "document_date"):
                    value = _norm_date(value)
                if value:
                    fields[field] = value
                    confidences[field] = 0.9 if len(value) >= 3 else 0.5
                break

    # Fallback: the client's own name when the document has no name line.
    if "legal_name" not in fields and client_name:
        fields["legal_name"] = client_name
        confidences["legal_name"] = 0.5
    return fields, confidences

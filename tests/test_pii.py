"""PII tests — no unmasked personal data in logs or model payloads."""

from app.agents import _tokenised_payload
from app.models import CaseCreate, CaseDocument, DocumentInput, OnboardingCase
from app.pii import mask_case_payload, mask_text


def test_mask_aadhaar_pan_passport_phone_email():
    text = (
        "Call Aarav Sharma on 98765 43210 or aarav@example.com. "
        "Aadhaar 2345 6789 0123, PAN ABCDE1234F, passport A1234567, "
        "CIN U74900MH2019PTC123456, account 12345678901234."
    )
    masked, detected = mask_text(text, extra_names=["Aarav Sharma"])
    assert "98765" not in masked
    assert "aarav@example.com" not in masked
    assert "2345" not in masked
    assert "ABCDE1234F" not in masked
    assert "A1234567" not in masked
    assert "U74900MH2019PTC123456" not in masked
    assert "12345678901234" not in masked
    assert "Aarav Sharma" not in masked
    assert set(detected) >= {"PHONE", "EMAIL", "AADHAAR", "PAN", "PASSPORT",
                             "CIN", "ACCOUNT", "NAME"}


def test_masked_payload_has_no_pii():
    payload = {
        "client_name": "Priya Patel",
        "contact_email": "priya@example.com",
        "contact_phone": "+91 98765 43210",
        "declared_registration_number": "U74900MH2019PTC123456",
        "declared_address": "45, Lakeview Apartments, Bengaluru",
        "documents": [{
            "name": "aadhaar_card.pdf",
            "content": "Name: Priya Patel\nAadhaar No.: 2345 6789 0123\n"
                       "PAN: ABCDE1234F\nValid Until: 2030-01-01",
        }],
    }
    safe = mask_case_payload(payload)
    blob = str(safe)
    for secret in ("Priya Patel", "priya@example.com", "98765",
                   "2345 6789 0123", "ABCDE1234F", "U74900MH2019PTC123456"):
        assert secret not in blob, f"PII leaked in masked payload: {secret}"


def test_tokenised_payload_has_no_real_names_or_addresses():
    """The payload sent to the model carries pseudonym tokens, not
    real names, addresses or registration numbers."""

    case = OnboardingCase(
        id="KYC-TEST", client_name="Priya Patel", client_type="individual",
        declared_registration_number="U74900MH2019PTC123456",
        declared_address="45, Lakeview Apartments, Bengaluru 560102",
    )
    case.documents.append(CaseDocument(
        id="DOC-1", name="aadhaar_card.pdf",
        content="Name: Priya Patel\nAadhaar No.: 2345 6789 0123",
        doc_type="id_proof",
        extracted_fields={
            "legal_name": "Priya Patel",
            "address": "45, Lakeview Apartments, Bengaluru 560102",
            "registration_number": "U74900MH2019PTC123456",
        },
    ))
    payload = _tokenised_payload(case)
    blob = str(payload)
    for secret in ("Priya Patel", "Lakeview Apartments",
                   "U74900MH2019PTC123456", "2345 6789 0123"):
        assert secret not in blob, f"PII leaked in model payload: {secret}"
    # ...but the tokens still let the model compare values.
    assert payload["documents"][0]["extracted_fields"]["legal_name"] == "NAME-1"
    assert payload["declared_registration_number"] == payload["documents"][0][
        "extracted_fields"]["registration_number"]


def test_trace_and_audit_contain_no_raw_pii(pipeline):
    case = pipeline.create_case(CaseCreate(
        client_name="Priya Patel", client_type="individual",
    ))
    pipeline.upload_document(case.id, DocumentInput(
        name="aadhaar_card.pdf",
        content="Name: Priya Patel\nAadhaar No.: 2345 6789 0123\n"
                "PAN: ABCDE1234F\nValid Until: 2030-01-01",
    ))
    stored = pipeline.get_case(case.id)
    trace_blob = str([step.model_dump() for step in stored.trace])
    audit_blob = str([entry.model_dump() for entry in stored.audit])
    for secret in ("2345 6789 0123", "ABCDE1234F"):
        assert secret not in trace_blob, f"PII in trace: {secret}"
        assert secret not in audit_blob, f"PII in audit: {secret}"


def test_pii_detected_recorded(pipeline):
    case = pipeline.create_case(CaseCreate(
        client_name="Priya Patel", client_type="individual",
    ))
    done = pipeline.upload_document(case.id, DocumentInput(
        name="aadhaar_card.pdf",
        content="Name: Priya Patel\nAadhaar No.: 2345 6789 0123\n"
                "Valid Until: 2030-01-01",
    ))
    assert "AADHAAR" in done.pii_detected

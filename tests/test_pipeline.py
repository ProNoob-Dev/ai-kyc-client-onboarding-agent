"""Pipeline tests — workflow behaviour without HTTP."""

from datetime import date, timedelta

from app.models import CaseCreate, DocumentInput

RECENT = (date.today() - timedelta(days=10)).isoformat()
FUTURE = (date.today() + timedelta(days=730)).isoformat()
PAST = (date.today() - timedelta(days=30)).isoformat()

ADDR = "12, Palm Court, Andheri West, Mumbai 400053"


def _case(pipeline, name="Priya Patel", ctype="individual", **kw):
    return pipeline.create_case(CaseCreate(
        client_name=name, client_type=ctype, **kw
    ))


def _upload(pipeline, case_id, name, content, hint=None):
    return pipeline.upload_document(case_id, DocumentInput(
        name=name, content=content, doc_type_hint=hint,
    ))


def _clean_individual(pipeline):
    case = _case(pipeline)
    _upload(pipeline, case.id, "aadhaar_card.pdf",
            f"Name: Priya Patel\nDate of Birth: 1988-11-02\n"
            f"Address: {ADDR}\nValid Until: {FUTURE}")
    return _upload(pipeline, case.id, "electricity_bill.pdf",
                    f"Name: Priya Patel\nAddress: {ADDR}\nBill Date: {RECENT}")


def test_clean_case_passes_through(pipeline):
    done = _clean_individual(pipeline)
    assert done.status == "ready"
    assert done.decision.value == "pass_through"
    assert done.requires_hitl is False
    assert done.mismatches == []
    assert done.extracted_fields["legal_name"] == "Priya Patel"
    assert len(done.citations) >= 1
    assert done.processing_duration_ms >= 0


def test_incomplete_case_waits_for_documents(pipeline):
    case = _case(pipeline)
    done = _upload(pipeline, case.id, "aadhaar_card.pdf",
                   f"Name: Priya Patel\nDate of Birth: 1988-11-02\n"
                   f"Address: {ADDR}\nValid Until: {FUTURE}")
    assert done.status == "pending_review"
    assert done.decision.value == "needs_review"
    assert any(m.field == "missing_document" for m in done.mismatches)


def test_expired_id_proposes_reject(pipeline):
    case = _case(pipeline, name="Nikhil Mehta")
    _upload(pipeline, case.id, "aadhaar_card.pdf",
            f"Name: Nikhil Mehta\nDate of Birth: 1986-08-23\n"
            f"Address: {ADDR}\nValid Until: {PAST}")
    done = _upload(pipeline, case.id, "electricity_bill.pdf",
                   f"Name: Nikhil Mehta\nAddress: {ADDR}\nBill Date: {RECENT}")
    assert done.status == "pending_review"
    assert done.decision.value == "propose_reject"
    assert any(m.field == "document_expiry" for m in done.mismatches)


def test_review_reject_is_the_only_way_to_reject(pipeline):
    case = _case(pipeline, name="Nikhil Mehta")
    _upload(pipeline, case.id, "aadhaar_card.pdf",
            f"Name: Nikhil Mehta\nDate of Birth: 1986-08-23\n"
            f"Address: {ADDR}\nValid Until: {PAST}")
    done = _upload(pipeline, case.id, "electricity_bill.pdf",
                   f"Name: Nikhil Mehta\nAddress: {ADDR}\nBill Date: {RECENT}")
    assert done.status == "pending_review"
    assert done.status != "rejected"  # agents can only propose

    rejected = pipeline.review_case(
        done.id, decision="reject", reviewer="kyc-officer-1",
        note="Expired ID.",
    )
    assert rejected.status == "rejected"
    assert rejected.reviewer == "kyc-officer-1"


def test_review_requires_pending_status(pipeline):
    import pytest

    done = _clean_individual(pipeline)
    assert done.status == "ready"
    with pytest.raises(ValueError):
        pipeline.review_case(done.id, decision="approve", reviewer="officer")


def test_review_queue_oldest_first(pipeline):
    first = _case(pipeline, name="First Person")
    _upload(pipeline, first.id, "aadhaar_card.pdf",
            f"Name: First Person\nValid Until: {PAST}")
    second = _case(pipeline, name="Second Person")
    _upload(pipeline, second.id, "aadhaar_card.pdf",
            f"Name: Second Person\nValid Until: {PAST}")
    queue = pipeline.review_queue()
    assert [c.id for c in queue] == [first.id, second.id]


def test_metrics(pipeline):
    _clean_individual(pipeline)
    metrics = pipeline.metrics()
    assert metrics["total_cases"] == 1
    assert metrics["ready"] == 1
    assert metrics["pass_through_rate"] == 1.0
    assert metrics["mismatch_rate"] == 0.0


def test_company_director_name_differs_from_company_name(pipeline):
    """A director's personal name on the ID proof must NOT flag a
    mismatch against the company name — only company documents are
    compared for name consistency."""
    case = _case(pipeline, name="Sharma Trading Pvt Ltd", ctype="company")
    _upload(pipeline, case.id, "certificate_of_incorporation.pdf",
            "Certificate of Incorporation\n"
            "Company Name: Sharma Trading Pvt Ltd\n"
            "CIN: U74900MH2019PTC123456\n"
            "Date of Incorporation: 2019-04-12\n"
            f"Registered Office: {ADDR}")
    _upload(pipeline, case.id, "aadhaar_card.pdf",
            f"Name: Aarav Sharma\nDate of Birth: 1990-05-14\n"
            f"Address: {ADDR}\nValid Until: {FUTURE}")
    done = _upload(pipeline, case.id, "electricity_bill.pdf",
                   f"Name: Sharma Trading Pvt Ltd\nAddress: {ADDR}\n"
                   f"Bill Date: {RECENT}")
    assert done.status == "ready"
    assert done.decision.value == "pass_through"

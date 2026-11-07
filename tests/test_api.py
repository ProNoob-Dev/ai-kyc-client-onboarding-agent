"""API tests — full HTTP round-trips through the FastAPI app."""

from datetime import date, timedelta

# Dates relative to today, so the tests stay correct whenever they run.
RECENT_BILL_DATE = (date.today() - timedelta(days=10)).isoformat()

CLEAN_INDIVIDUAL = {
    "client_name": "Priya Patel",
    "client_type": "individual",
    "country": "India",
}

ID_PROOF = {
    "name": "aadhaar_card.pdf",
    "content": (
        "Government of India — Aadhaar Card\n"
        "Name: Priya Patel\n"
        "Aadhaar No.: 2345 6789 0123\n"
        "Date of Birth: 1988-11-02\n"
        "Address: 45, Lakeview Apartments, HSR Layout, Bengaluru 560102\n"
        "Valid Until: 2030-01-01"
    ),
}

ADDRESS_PROOF = {
    "name": "electricity_bill.pdf",
    "content": (
        "Electricity Bill — Maharashtra State Electricity Board\n"
        "Name: Priya Patel\n"
        "Address: 45, Lakeview Apartments, HSR Layout, Bengaluru 560102\n"
        f"Bill Date: {RECENT_BILL_DATE}\n"
        "Amount due: Rs. 2,340"
    ),
}


def _clean_case(client):
    case = client.post("/onboarding", json=CLEAN_INDIVIDUAL).json()
    client.post(f"/onboarding/{case['id']}/documents", json=ID_PROOF)
    done = client.post(
        f"/onboarding/{case['id']}/documents", json=ADDRESS_PROOF
    ).json()
    return done


def test_create_case(client):
    resp = client.post("/onboarding", json=CLEAN_INDIVIDUAL)
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_name"] == "Priya Patel"
    assert body["client_type"] == "individual"
    assert body["status"] == "submitted"
    assert body["required_documents"] == ["id_proof", "address_proof"]


def test_company_requires_three_documents(client):
    resp = client.post("/onboarding", json={
        "client_name": "Sharma Trading Pvt Ltd",
        "client_type": "company",
    })
    assert resp.status_code == 201
    assert resp.json()["required_documents"] == [
        "incorporation", "id_proof", "address_proof",
    ]


def test_clean_case_passes_through(client):
    done = _clean_case(client)
    assert done["status"] == "ready"
    assert done["decision"] == "pass_through"
    assert done["requires_hitl"] is False
    assert done["mismatches"] == []
    assert len(done["citations"]) >= 1
    assert len(done["trace"]) == 10  # 5 steps per upload × 2 documents
    assert done["documents"][0]["doc_type"] == "id_proof"
    assert done["documents"][1]["doc_type"] == "address_proof"


def test_case_status_endpoint(client):
    done = _clean_case(client)
    resp = client.get(f"/onboarding/{done['id']}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["client_name"] == "Priya Patel"
    assert len(body["documents"]) == 2
    assert body["mismatches"] == []
    assert body["extracted_fields"]["legal_name"] == "Priya Patel"


def test_get_missing_case_404(client):
    assert client.get("/onboarding/KYC-DOESNOTEXIST").status_code == 404
    assert client.post(
        "/onboarding/KYC-DOESNOTEXIST/documents", json=ID_PROOF
    ).status_code == 404


def test_mismatch_goes_to_review_queue(client):
    case = client.post("/onboarding", json={
        "client_name": "Vikram Khan",
        "client_type": "individual",
    }).json()
    client.post(f"/onboarding/{case['id']}/documents", json={
        "name": "aadhaar_card.pdf",
        "content": (
            "Government of India — Aadhaar Card\n"
            "Name: Vikram Khan\n"
            "Date of Birth: 1991-12-05\n"
            "Address: 12, Palm Court, Andheri West, Mumbai 400053\n"
            "Valid Until: 2030-01-01"
        ),
    })
    done = client.post(f"/onboarding/{case['id']}/documents", json={
        "name": "electricity_bill.pdf",
        "content": (
            "Electricity Bill\n"
            "Name: Vikram Khaan\n"
            "Address: 12, Palm Court, Andheri West, Mumbai 400053\n"
            f"Bill Date: {RECENT_BILL_DATE}"
        ),
    }).json()
    assert done["status"] == "pending_review"
    assert done["decision"] == "needs_review"
    assert done["requires_hitl"] is True
    assert len(done["mismatches"]) == 1
    assert done["mismatches"][0]["field"] == "legal_name"
    assert done["mismatches"][0]["severity"] == "high"

    queue = client.get("/onboarding/review-queue").json()
    assert any(c["id"] == case["id"] for c in queue)


def test_review_flow_end_to_end(client):
    case = client.post("/onboarding", json={
        "client_name": "Vikram Khan",
        "client_type": "individual",
    }).json()
    client.post(f"/onboarding/{case['id']}/documents", json={
        "name": "aadhaar_card.pdf",
        "content": (
            "Name: Vikram Khan\nDate of Birth: 1991-12-05\n"
            "Address: 12, Palm Court, Andheri West, Mumbai 400053\n"
            "Valid Until: 2030-01-01"
        ),
    })
    done = client.post(f"/onboarding/{case['id']}/documents", json={
        "name": "electricity_bill.pdf",
        "content": (
            "Name: Vikram Khaan\n"
            "Address: 12, Palm Court, Andheri West, Mumbai 400053\n"
            f"Bill Date: {RECENT_BILL_DATE}"
        ),
    }).json()
    assert done["status"] == "pending_review"

    reviewed = client.post(
        f"/onboarding/{case['id']}/review/approve",
        json={"reviewer": "kyc-officer-1", "note": "Name typo confirmed."},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "approved"
    assert reviewed.json()["reviewer"] == "kyc-officer-1"
    assert reviewed.json()["mismatches"][0]["status"] == "accepted"

    # Reviewing an already-decided case is a conflict.
    again = client.post(
        f"/onboarding/{case['id']}/review/reject",
        json={"reviewer": "kyc-officer-1"},
    )
    assert again.status_code == 409


def test_review_reject_and_request_info(client):
    case = client.post("/onboarding", json={
        "client_name": "Nikhil Mehta",
        "client_type": "individual",
    }).json()
    client.post(f"/onboarding/{case['id']}/documents", json={
        "name": "aadhaar_card.pdf",
        "content": (
            "Name: Nikhil Mehta\nDate of Birth: 1986-08-23\n"
            "Address: 12, Palm Court, Andheri West, Mumbai 400053\n"
            "Valid Until: 2020-01-01"
        ),
    })
    done = client.post(f"/onboarding/{case['id']}/documents", json={
        "name": "electricity_bill.pdf",
        "content": (
            "Name: Nikhil Mehta\n"
            "Address: 12, Palm Court, Andheri West, Mumbai 400053\n"
            f"Bill Date: {RECENT_BILL_DATE}"
        ),
    }).json()
    assert done["status"] == "pending_review"
    assert done["decision"] == "propose_reject"  # expired ID

    rejected = client.post(
        f"/onboarding/{case['id']}/review/reject",
        json={"reviewer": "kyc-officer-2", "note": "Expired ID, cannot onboard."},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"


def test_invalid_review_action(client):
    done = _clean_case(client)
    resp = client.post(
        f"/onboarding/{done['id']}/review/banana",
        json={"reviewer": "kyc-officer-1"},
    )
    assert resp.status_code == 400


def test_metrics_endpoint(client):
    _clean_case(client)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cases"] >= 1
    assert body["ready"] >= 1
    assert 0.0 <= body["pass_through_rate"] <= 1.0


def test_audit_endpoint(client):
    done = _clean_case(client)
    resp = client.get("/audit", params={"case_id": done["id"]})
    assert resp.status_code == 200
    actions = [entry["action"] for entry in resp.json()]
    assert "case-created" in actions
    assert "onboarding-decision" in actions

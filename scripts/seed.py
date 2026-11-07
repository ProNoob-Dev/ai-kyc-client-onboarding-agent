"""Seed data: 30 sample onboarding cases + a labelled golden set of 20.

Run ``python scripts/seed.py`` to (re)generate ``data/sample_cases.json``
and ``data/golden_set.json``. Run ``python scripts/seed.py --submit`` to
also create every sample case and upload its documents to a locally
running API (http://localhost:8000 by default).

The golden set is the eval set: each case carries the routing the
pipeline is expected to produce. ``tests/test_evals.py`` runs the
pipeline over it and asserts >= 88% correct routing, zero final
rejections without human review, and a citation on every decision.

Date handling: dates in the seed data are written as relative tokens
such as ``{today-10d}`` or ``{today+2y}`` and resolved to real dates at
load time (by the tests and by --submit). That keeps the golden set
correct whenever it runs — a "recent" address proof stays recent, an
"expired" ID stays expired.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

_DATE_TOKEN = re.compile(r"\{today([+-])(\d+)([dy])\}")


def resolve_dates(obj):
    """Recursively resolve {today-10d} / {today+2y} tokens to ISO dates."""
    if isinstance(obj, str):
        def _replace(match: re.Match) -> str:
            sign, amount, unit = match.groups()
            delta = timedelta(days=int(amount) * (365 if unit == "y" else 1))
            day = date.today() + delta if sign == "+" else date.today() - delta
            return day.isoformat()
        return _DATE_TOKEN.sub(_replace, obj)
    if isinstance(obj, list):
        return [resolve_dates(item) for item in obj]
    if isinstance(obj, dict):
        return {key: resolve_dates(value) for key, value in obj.items()}
    return obj


FIRST = ["Aarav", "Priya", "Rohan", "Sneha", "Vikram", "Ananya", "Karan",
         "Meera", "Arjun", "Divya", "Nikhil", "Kavya", "Aditya", "Riya",
         "Sanjay", "Pooja", "Manish", "Ishita", "Varun", "Nisha"]
LAST = ["Sharma", "Patel", "Iyer", "Reddy", "Khan", "Gupta", "Nair", "Singh",
        "Das", "Joshi", "Mehta", "Kulkarni", "Chopra", "Menon", "Rao"]
COMPANY_SUFFIX = ["Trading Pvt Ltd", "Textiles Pvt Ltd", "Logistics LLP",
                  "Foods Pvt Ltd", "Technologies Pvt Ltd", "Exports LLP"]


def _doc(name: str, content: str, doc_type_hint: str | None = None) -> dict:
    doc = {"name": name, "content": content, "content_type": "text/plain"}
    if doc_type_hint:
        doc["doc_type_hint"] = doc_type_hint
    return doc


def _id_proof(name: str, id_no: str, dob: str, expiry: str,
              address: str) -> dict:
    return _doc(
        "aadhaar_card.pdf",
        f"Government of India — Aadhaar Card\n"
        f"Name: {name}\n"
        f"Aadhaar No.: 2345 6789 0123\n"
        f"Date of Birth: {dob}\n"
        f"Address: {address}\n"
        f"Valid Until: {expiry}",
        doc_type_hint="id_proof",
    )


def _incorporation(company: str, cin: str, inc_date: str,
                   address: str) -> dict:
    return _doc(
        "certificate_of_incorporation.pdf",
        f"Certificate of Incorporation\n"
        f"Company Name: {company}\n"
        f"CIN: {cin}\n"
        f"Date of Incorporation: {inc_date}\n"
        f"Registered Office: {address}\n"
        f"Registrar of Companies, Mumbai",
        doc_type_hint="incorporation",
    )


def _address_proof(name: str, address: str, bill_date: str) -> dict:
    return _doc(
        "electricity_bill.pdf",
        f"Electricity Bill — Maharashtra State Electricity Board\n"
        f"Name: {name}\n"
        f"Address: {address}\n"
        f"Bill Date: {bill_date}\n"
        f"Amount due: Rs. 2,340",
        doc_type_hint="address_proof",
    )


def _case(client_name: str, client_type: str, documents: list[dict],
          **overrides) -> dict:
    base = {
        "client_name": client_name,
        "client_type": client_type,
        "country": "India",
        "documents": documents,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Golden set (20) — labelled expected routing
# ---------------------------------------------------------------------------
def golden_set() -> list[dict]:
    cases: list[dict] = []

    def add(case_id: str, expected: str, case: dict) -> None:
        case["case_id"] = case_id
        case["expected_decision"] = expected
        cases.append(case)

    addr1 = "12, Palm Court, Andheri West, Mumbai 400053"
    addr2 = "45, Lakeview Apartments, HSR Layout, Bengaluru 560102"

    # --- pass-through: clean, all documents consistent ---
    add("G01-company-clean", "pass_through", _case(
        "Sharma Trading Pvt Ltd", "company", [
            _incorporation("Sharma Trading Pvt Ltd", "U74900MH2019PTC123456",
                           "{today-5y}", addr1),
            _id_proof("Aarav Sharma", "ABCDE1234F", "1990-05-14",
                      "{today+2y}", addr1),
            _address_proof("Sharma Trading Pvt Ltd", addr1, "{today-10d}"),
        ], declared_registration_number="U74900MH2019PTC123456",
        declared_address=addr1))
    add("G02-individual-clean", "pass_through", _case(
        "Priya Patel", "individual", [
            _id_proof("Priya Patel", "PQRSX9876K", "1988-11-02",
                      "{today+2y}", addr2),
            _address_proof("Priya Patel", addr2, "{today-10d}"),
        ]))
    add("G03-company-clean-2", "pass_through", _case(
        "Meera Textiles Pvt Ltd", "company", [
            _incorporation("Meera Textiles Pvt Ltd", "U17299GJ2020PTC098765",
                           "{today-4y}", addr2),
            _id_proof("Meera Singh", "LMNOP4321Q", "1985-03-21",
                      "{today+2y}", addr2),
            _address_proof("Meera Textiles Pvt Ltd", addr2, "{today-5d}"),
        ], declared_registration_number="U17299GJ2020PTC098765"))
    add("G04-individual-clean-2", "pass_through", _case(
        "Rohan Iyer", "individual", [
            _id_proof("Rohan Iyer", "RTYUI5678Z", "1992-07-30",
                      "{today+2y}", addr1),
            _address_proof("Rohan Iyer", addr1, "{today-10d}"),
        ]))
    add("G05-company-clean-3", "pass_through", _case(
        "Karan Logistics LLP", "company", [
            _incorporation("Karan Logistics LLP", "AAB-1234",
                           "{today-3y}", addr1),
            _id_proof("Karan Nair", "KLMNO1122R", "1987-01-19",
                      "{today+2y}", addr1),
            _address_proof("Karan Logistics LLP", addr1, "{today-10d}"),
        ]))
    add("G06-individual-clean-3", "pass_through", _case(
        "Sneha Reddy", "individual", [
            _id_proof("Sneha Reddy", "SDFGH3456T", "1995-09-08",
                      "{today+2y}", addr2),
            _address_proof("Sneha Reddy", addr2, "{today-10d}"),
        ]))

    # --- needs review: name mismatch across documents ---
    add("G07-name-mismatch", "needs_review", _case(
        "Vikram Khan", "individual", [
            _id_proof("Vikram Khan", "VBNMK0987L", "1991-12-05",
                      "{today+2y}", addr1),
            _address_proof("Vikram Khaan", addr1, "{today-10d}"),
        ]))
    add("G08-company-name-mismatch", "needs_review", _case(
        "Ananya Foods Pvt Ltd", "company", [
            _incorporation("Ananya Foods Pvt Ltd", "U15400DL2018PTC334455",
                           "{today-6y}", addr1),
            _id_proof("Ananya Gupta", "ANANY1234G", "1989-04-11",
                      "{today+2y}", addr1),
            _address_proof("Ananya Food Products Pvt Ltd", addr1, "{today-10d}"),
        ]))

    # --- needs review: registration number mismatch ---
    add("G09-registration-mismatch", "needs_review", _case(
        "Arjun Technologies Pvt Ltd", "company", [
            _incorporation("Arjun Technologies Pvt Ltd", "U72900KA2021PTC111222",
                           "{today-2y}", addr2),
            _id_proof("Arjun Das", "ARJUN5678D", "1993-06-17",
                      "{today+2y}", addr2),
            _address_proof("Arjun Technologies Pvt Ltd", addr2, "{today-10d}"),
        ], declared_registration_number="U72900KA2021PTC999999"))

    # --- needs review: address mismatch ---
    add("G10-address-mismatch", "needs_review", _case(
        "Divya Joshi", "individual", [
            _id_proof("Divya Joshi", "DIVYA2468J", "1994-02-28",
                      "{today+2y}", addr1),
            _address_proof("Divya Joshi", addr2, "{today-10d}"),
        ]))

    # --- propose reject: expired ID ---
    add("G11-id-expired", "propose_reject", _case(
        "Nikhil Mehta", "individual", [
            _id_proof("Nikhil Mehta", "NIKHIL1357M", "1986-08-23",
                      "{today-30d}", addr1),
            _address_proof("Nikhil Mehta", addr1, "{today-10d}"),
        ]))
    add("G12-id-expired-company", "propose_reject", _case(
        "Kavya Exports LLP", "company", [
            _incorporation("Kavya Exports LLP", "AAB-5678",
                           "{today-3y}", addr2),
            _id_proof("Kavya Kulkarni", "KAVYA8642K", "1990-10-09",
                      "{today-30d}", addr2),
            _address_proof("Kavya Exports LLP", addr2, "{today-10d}"),
        ]))

    # --- needs review: missing required documents ---
    add("G13-missing-documents", "needs_review", _case(
        "Aditya Chopra", "individual", [
            _id_proof("Aditya Chopra", "ADITYA9753C", "1992-04-03",
                      "{today+2y}", addr1),
        ]))
    add("G14-company-missing-address", "needs_review", _case(
        "Riya Menon Exports Pvt Ltd", "company", [
            _incorporation("Riya Menon Exports Pvt Ltd", "U51909KL2022PTC778899",
                           "{today-1y}", addr2),
            _id_proof("Riya Menon", "RIYAM3579N", "1988-12-12",
                      "{today+2y}", addr2),
        ]))

    # --- needs review: address proof too old ---
    add("G15-address-proof-old", "needs_review", _case(
        "Sanjay Rao", "individual", [
            _id_proof("Sanjay Rao", "SANJAY7531R", "1984-05-27",
                      "{today+2y}", addr1),
            _address_proof("Sanjay Rao", addr1, "{today-200d}"),
        ]))

    # --- needs review: future incorporation date ---
    add("G16-incorporation-future", "needs_review", _case(
        "Pooja Sharma Technologies Pvt Ltd", "company", [
            _incorporation("Pooja Sharma Technologies Pvt Ltd",
                           "U62099MH2024PTC445566", "{today+1y}", addr1),
            _id_proof("Pooja Sharma", "POOJA6428S", "1991-01-15",
                      "{today+2y}", addr1),
            _address_proof("Pooja Sharma Technologies Pvt Ltd", addr1,
                           "{today-10d}"),
        ]))

    # --- needs review: unclassifiable document ---
    add("G17-unknown-document", "needs_review", _case(
        "Manish Patel", "individual", [
            _id_proof("Manish Patel", "MANISH2468P", "1989-09-19",
                      "{today+2y}", addr1),
            _doc("random_note.txt",
                 "Meeting notes: call the client on Tuesday to discuss "
                 "the onboarding timeline and next steps."),
        ]))

    # --- needs review: multiple mismatches at once ---
    add("G18-multiple-mismatches", "needs_review", _case(
        "Ishita Iyer", "individual", [
            _id_proof("Ishita Iyer", "ISHITA8642I", "1993-03-08",
                      "{today+2y}", addr1),
            _address_proof("Ishita Iyyer", addr2, "{today-200d}"),
        ]))

    # --- needs review: declared address differs ---
    add("G19-declared-address-mismatch", "needs_review", _case(
        "Varun Reddy", "individual", [
            _id_proof("Varun Reddy", "VARUN1357V", "1990-07-22",
                      "{today+2y}", addr1),
            _address_proof("Varun Reddy", addr1, "{today-10d}"),
        ], declared_address=addr2))

    # --- needs review: declared registration differs ---
    add("G20-declared-registration-mismatch", "needs_review", _case(
        "Nisha Khan Foods Pvt Ltd", "company", [
            _incorporation("Nisha Khan Foods Pvt Ltd", "U15400MH2020PTC667788",
                           "{today-3y}", addr1),
            _id_proof("Nisha Khan", "NISHA9753N", "1987-11-30",
                      "{today+2y}", addr1),
            _address_proof("Nisha Khan Foods Pvt Ltd", addr1, "{today-10d}"),
        ], declared_registration_number="U15400MH2020PTC000000"))

    return cases


# ---------------------------------------------------------------------------
# Sample cases (30) — realistic variety for demos and manual testing
# ---------------------------------------------------------------------------
def sample_cases(n: int = 30, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    cases: list[dict] = []
    addr_pool = [
        "12, Palm Court, Andheri West, Mumbai 400053",
        "45, Lakeview Apartments, HSR Layout, Bengaluru 560102",
        "7, Rosewood Enclave, Gurugram 122001",
        "23, Marina Towers, Chennai 600028",
    ]
    for i in range(n):
        company = rng.random() < 0.5
        addr = addr_pool[i % len(addr_pool)]
        if company:
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)} {rng.choice(COMPANY_SUFFIX)}"
            cin = f"U{rng.randint(10000, 99999)}{rng.choice(['MH', 'KA', 'DL'])}20{rng.randint(15, 24)}PTC{rng.randint(100000, 999999)}"
            docs = [
                _incorporation(name, cin, "{today-4y}", addr),
                _id_proof(f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                          "ABCDE1234F", "1990-05-14", "{today+2y}", addr),
                _address_proof(name, addr, "{today-10d}"),
            ]
            cases.append(_case(name, "company", docs,
                               declared_registration_number=cin,
                               declared_address=addr))
        else:
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            docs = [
                _id_proof(name, "ABCDE1234F", "1990-05-14", "{today+2y}", addr),
                _address_proof(name, addr, "{today-10d}"),
            ]
            cases.append(_case(name, "individual", docs))
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate seed data.")
    parser.add_argument("--submit", action="store_true",
                        help="Create sample cases on a running API")
    parser.add_argument("--api", default="http://localhost:8000",
                        help="API base URL for --submit")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    golden = golden_set()
    samples = sample_cases()
    with open(os.path.join(DATA_DIR, "golden_set.json"), "w",
              encoding="utf-8") as fh:
        json.dump(golden, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(DATA_DIR, "sample_cases.json"), "w",
              encoding="utf-8") as fh:
        json.dump(samples, fh, indent=2, ensure_ascii=False)
    print(f"Wrote {len(golden)} golden cases and {len(samples)} sample cases.")

    if args.submit:
        import urllib.request

        for case in samples:
            resolved = resolve_dates(case)
            documents = resolved.pop("documents")
            req = urllib.request.Request(
                args.api.rstrip("/") + "/onboarding",
                data=json.dumps(resolved).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                created = json.loads(resp.read())
            for doc in documents:
                req = urllib.request.Request(
                    args.api.rstrip("/") + f"/onboarding/{created['id']}/documents",
                    data=json.dumps(doc).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
            print(f"  {created['id']}: {created['client_name']} "
                  f"-> {result['status']} ({result['decision']})")


if __name__ == "__main__":
    main()

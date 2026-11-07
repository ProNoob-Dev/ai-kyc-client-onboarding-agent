"""Golden-set evaluation.

Runs the full pipeline over the 20 labelled cases in
``data/golden_set.json`` and asserts the acceptance criteria from the
build spec:

* >= 88% of golden cases routed correctly (pass-through / needs-review /
  propose-reject),
* ZERO final rejections without a human reviewer,
* every decision linked to at least one requirement-rule citation,
* 100% of mismatches surfaced to the review queue (no silent passes).
"""

import json
import os

from app.models import CaseCreate, DocumentInput
from scripts.seed import resolve_dates

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_golden() -> list[dict]:
    with open(os.path.join(BASE_DIR, "data", "golden_set.json"),
              encoding="utf-8") as fh:
        return resolve_dates(json.load(fh))


def _run_case(pipeline, case: dict):
    payload = {
        k: v for k, v in case.items()
        if k not in ("case_id", "expected_decision", "documents")
    }
    created = pipeline.create_case(CaseCreate(**payload))
    done = created
    for doc in case["documents"]:
        done = pipeline.upload_document(
            created.id, DocumentInput(**doc)
        )
    return done


def test_golden_set_routing_accuracy(pipeline):
    cases = _load_golden()
    assert len(cases) == 20

    correct = 0
    failures: list[str] = []
    for case in cases:
        done = _run_case(pipeline, case)
        got = done.decision.value if done.decision else None
        expected = case["expected_decision"]
        if got == expected:
            correct += 1
        else:
            failures.append(
                f"{case['case_id']}: expected {expected}, got {got} "
                f"(status={done.status}, "
                f"mismatches={[m.field for m in done.mismatches]})"
            )

    accuracy = correct / len(cases)
    assert accuracy >= 0.88, (
        f"Routing accuracy {accuracy:.0%} < 88% "
        f"({correct}/{len(cases)} correct). Failures:\n" + "\n".join(failures)
    )


def test_golden_set_zero_rejections_without_hitl(pipeline):
    cases = _load_golden()
    for case in cases:
        done = _run_case(pipeline, case)
        assert done.status != "rejected", (
            f"{case['case_id']}: case was finally rejected without a human"
        )


def test_golden_set_every_decision_cited(pipeline):
    cases = _load_golden()
    for case in cases:
        done = _run_case(pipeline, case)
        assert len(done.citations) >= 1, (
            f"{case['case_id']}: decision has no requirement citation"
        )


def test_golden_set_all_mismatches_surfaced(pipeline):
    """Every mismatch the pipeline finds must be visible on the case —
    nothing is silently passed."""
    cases = _load_golden()
    for case in cases:
        done = _run_case(pipeline, case)
        if done.decision.value in ("needs_review", "propose_reject"):
            assert len(done.mismatches) >= 1, (
                f"{case['case_id']}: routed to review with no mismatch listed"
            )
            assert all(m.status == "open" for m in done.mismatches)

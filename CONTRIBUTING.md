# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

## Rules that matter here

1. **A rejection is only ever a proposal until a human confirms it.**
   The supervisor may route a case to `propose_reject`, but the case
   only becomes `rejected` through `POST /onboarding/{id}/review/reject`
   by a named reviewer. Never change this in code — if a new rule needs
   a rejection path, it goes through the review queue.

2. **PII is masked at ingestion, before any model call.** New document
   types and new extracted fields must be covered by `app/pii.py`
   (`mask_text` / `mask_case_payload`) and by a test in
   `tests/test_pii.py` that proves no unmasked value reaches a payload
   or a log line. The extraction step itself never calls an LLM —
   names, addresses and ID numbers are extracted with regex only.

3. **Every mismatch is surfaced.** If the pipeline finds a mismatch,
   it must appear on the case and in the review queue. A decision of
   `needs_review` / `propose_reject` with zero listed mismatches is a
   bug, and `tests/test_evals.py` will catch it.

4. **Every decision is cited.** Each case links to at least one KYC
   requirement rule (KYC-R01–KYC-R10 in `data/requirements.json`).
   New rules go in that file with a stable ID, title and excerpt.

5. **Seed data stays date-relative.** Golden-set and sample dates use
   `{today±Nd}` tokens resolved at load time (`scripts/seed.py`),
   never fixed calendar dates — otherwise the eval rots.

## Tests

```bash
ruff check app scripts tests
pytest -q
```

CI runs both plus a smoke test that boots the API and submits a fresh
case. Add a test for any new endpoint, rule, or PII pattern. Keep the
golden set green — routing accuracy must stay at or above 88%.

## Screenshots

If you change the dashboard, re-capture the screenshots so the README
stays honest:

```bash
uvicorn app.main:app &           # in one terminal
python scripts/seed.py --submit  # in another
python scripts/screenshot.py
```

Screenshots are taken from the real running app with Playwright —
never mock them up.

"""Postgres-backed case store (production persistence).

Used instead of the JSON store (``app/store.py``) when ``DATABASE_URL``
is set. Same interface as :class:`app.store.CaseStore`, so the pipeline
never knows which backend it is talking to.

Tables are created on first use (``CREATE TABLE IF NOT EXISTS``).
Cases are stored as JSONB rows, so the schema stays stable as the
case model evolves; the audit log is a separate append-only table.

NOTE: the Postgres path is not integration-tested on a developer laptop
without a local Postgres (see README limitations). The JSON store is
the default and is fully tested.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid

from app.models import AuditEntry, OnboardingCase

logger = logging.getLogger(__name__)

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS onboarding_cases (
    id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS onboarding_audit (
    id SERIAL PRIMARY KEY,
    case_id TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_case_id ON onboarding_audit (case_id);
"""


class PostgresCaseStore:
    def __init__(self, database_url: str) -> None:
        import psycopg

        self._conn = psycopg.connect(database_url, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute(_SCHEMA)

    # -- ids ------------------------------------------------------------------
    def new_id(self) -> str:
        return f"KYC-{uuid.uuid4().hex[:8].upper()}"

    def new_doc_id(self) -> str:
        return f"DOC-{uuid.uuid4().hex[:8].upper()}"

    def new_mismatch_id(self) -> str:
        return f"MM-{uuid.uuid4().hex[:8].upper()}"

    # -- CRUD -----------------------------------------------------------------
    def create(self, case: OnboardingCase) -> OnboardingCase:
        with _lock, self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO onboarding_cases (id, data) VALUES (%s, %s::jsonb)",
                (case.id, json.dumps(case.model_dump(), ensure_ascii=False)),
            )
        return case

    def get(self, case_id: str) -> OnboardingCase | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM onboarding_cases WHERE id = %s", (case_id,)
            )
            row = cur.fetchone()
        return OnboardingCase(**row[0]) if row else None

    def update(self, case: OnboardingCase) -> OnboardingCase:
        with _lock, self._conn.cursor() as cur:
            cur.execute(
                "UPDATE onboarding_cases SET data = %s::jsonb, "
                "updated_at = now() WHERE id = %s",
                (json.dumps(case.model_dump(), ensure_ascii=False), case.id),
            )
        return case

    def list_all(self) -> list[OnboardingCase]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM onboarding_cases ORDER BY created_at DESC"
            )
            return [OnboardingCase(**row[0]) for row in cur.fetchall()]

    # -- audit ------------------------------------------------------------------
    def audit(self, case: OnboardingCase, *, actor: str, action: str, detail: str) -> None:
        entry = AuditEntry(actor=actor, action=action, detail=detail)
        case.audit.append(entry)
        with _lock, self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO onboarding_audit (case_id, at, actor, action, detail)"
                " VALUES (%s, %s, %s, %s, %s)",
                (case.id, entry.at, actor, action, detail),
            )
        self.update(case)

    def audit_entries(self, case_id: str | None = None) -> list[dict]:
        with self._conn.cursor() as cur:
            if case_id:
                cur.execute(
                    "SELECT case_id, at, actor, action, detail "
                    "FROM onboarding_audit WHERE case_id = %s ORDER BY id",
                    (case_id,),
                )
            else:
                cur.execute(
                    "SELECT case_id, at, actor, action, detail "
                    "FROM onboarding_audit ORDER BY id"
                )
            return [
                {
                    "case_id": row[0],
                    "at": row[1].isoformat() if hasattr(row[1], "isoformat") else row[1],
                    "actor": row[2],
                    "action": row[3],
                    "detail": row[4],
                }
                for row in cur.fetchall()
            ]


def build_case_store(database_url: str, cases_file: str, audit_file: str):
    """Return the Postgres store when DATABASE_URL is set and reachable,
    otherwise fall back to the JSON store with a warning."""
    if database_url:
        try:
            store = PostgresCaseStore(database_url)
            logger.info("Case store: Postgres (%s)", database_url.split("@")[-1])
            return store
        except Exception as exc:  # noqa: BLE001 — any failure means "no Postgres"
            logger.warning(
                "Postgres unreachable (%s) — using the local JSON store.", exc
            )
    from app.store import CaseStore

    return CaseStore(cases_file, audit_file)

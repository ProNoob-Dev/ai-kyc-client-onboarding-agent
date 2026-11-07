"""Onboarding case store + audit log.

Cases live in a small JSON file (``data/cases.json``) so the demo
survives restarts without a database. Every decision and every human
review is appended to an audit log — both on the case record and in
``data/audit_log.jsonl`` — so any decision can be traced back to its
source documents and the requirement rule that justified it.

Production deployments should set ``DATABASE_URL`` to use the Postgres
store instead (see ``app/pg_store.py``).
"""

from __future__ import annotations

import json
import os
import threading
import uuid

from app.models import AuditEntry, OnboardingCase

_lock = threading.Lock()


class CaseStore:
    def __init__(self, cases_file: str, audit_file: str) -> None:
        self._cases_file = cases_file
        self._audit_file = audit_file
        os.makedirs(os.path.dirname(cases_file) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(audit_file) or ".", exist_ok=True)
        self._cases: dict[str, OnboardingCase] = {}
        self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        if os.path.exists(self._cases_file):
            try:
                with open(self._cases_file, encoding="utf-8") as fh:
                    raw = json.load(fh)
                self._cases = {
                    item["id"]: OnboardingCase(**item) for item in raw
                }
            except (json.JSONDecodeError, KeyError, TypeError):
                self._cases = {}

    def _save(self) -> None:
        tmp = self._cases_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                [c.model_dump() for c in self._cases.values()],
                fh,
                indent=2,
                ensure_ascii=False,
            )
        os.replace(tmp, self._cases_file)

    # -- CRUD ---------------------------------------------------------------
    def new_id(self) -> str:
        return f"KYC-{uuid.uuid4().hex[:8].upper()}"

    def new_doc_id(self) -> str:
        return f"DOC-{uuid.uuid4().hex[:8].upper()}"

    def new_mismatch_id(self) -> str:
        return f"MM-{uuid.uuid4().hex[:8].upper()}"

    def create(self, case: OnboardingCase) -> OnboardingCase:
        with _lock:
            self._cases[case.id] = case
            self._save()
        return case

    def get(self, case_id: str) -> OnboardingCase | None:
        return self._cases.get(case_id)

    def update(self, case: OnboardingCase) -> OnboardingCase:
        with _lock:
            self._cases[case.id] = case
            self._save()
        return case

    def list_all(self) -> list[OnboardingCase]:
        return sorted(
            self._cases.values(), key=lambda c: c.created_at, reverse=True
        )

    # -- audit --------------------------------------------------------------
    def audit(self, case: OnboardingCase, *, actor: str, action: str, detail: str) -> None:
        entry = AuditEntry(actor=actor, action=action, detail=detail)
        case.audit.append(entry)
        with _lock:
            with open(self._audit_file, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "case_id": case.id,
                            "at": entry.at,
                            "actor": actor,
                            "action": action,
                            "detail": detail,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            self._save()

    def audit_entries(self, case_id: str | None = None) -> list[dict]:
        if not os.path.exists(self._audit_file):
            return []
        entries = []
        with open(self._audit_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if case_id is None or item.get("case_id") == case_id:
                    entries.append(item)
        return entries

"""Test fixtures.

Tests run against a temporary case store (never the repo's data/ files)
and the deterministic mock LLM provider, so they are fully offline and
reproducible.
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="kyc_onboarding_test_")
os.environ["CASES_DB_FILE"] = os.path.join(_TMP, "cases.json")
os.environ["AUDIT_LOG_FILE"] = os.path.join(_TMP, "audit.jsonl")
os.environ["LLM_PROVIDER"] = "mock"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.pipeline import OnboardingPipeline
from app.store import CaseStore


@pytest.fixture()
def pipeline(tmp_path):
    store = CaseStore(
        str(tmp_path / "cases.json"), str(tmp_path / "audit.jsonl")
    )
    return OnboardingPipeline(store=store)


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        # Fresh store per test: the API's case store is file-backed and
        # shared across tests, so clear it to avoid interference between
        # tests using the same client names.
        import app.main as main_module

        store = main_module.pipeline.store
        store._cases.clear()
        store._save()
        if os.path.exists(store._audit_file):
            os.remove(store._audit_file)
        yield test_client

"""Configuration for the KYC & Client Onboarding Document Agent.

All settings come from environment variables (see .env.example).
No secrets are hardcoded anywhere in this project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Application settings, loaded from the environment."""

    # LLM provider: "mock" (default, offline, deterministic) or "azure"
    llm_provider: str = os.environ.get("LLM_PROVIDER", "mock").lower()

    # Azure OpenAI (only used when LLM_PROVIDER=azure)
    azure_openai_endpoint: str = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    azure_openai_api_key: str = os.environ.get("AZURE_OPENAI_API_KEY", "")
    azure_openai_deployment: str = os.environ.get(
        "AZURE_OPENAI_DEPLOYMENT", "gpt-4o"
    )
    azure_openai_api_version: str = os.environ.get(
        "AZURE_OPENAI_API_VERSION", "2024-02-01"
    )

    # Azure Document Intelligence (only used when configured; the API
    # accepts pre-extracted document text, so onboarding works offline)
    azure_docintel_endpoint: str = os.environ.get(
        "AZURE_DOCINTEL_ENDPOINT", ""
    )
    azure_docintel_api_key: str = os.environ.get("AZURE_DOCINTEL_API_KEY", "")

    # Persistence: Postgres when DATABASE_URL is set (see app/pg_store.py),
    # otherwise a local JSON store (see app/store.py).
    database_url: str = os.environ.get("DATABASE_URL", "")

    # Triage policy
    # Address proof must be dated within this many days of submission.
    address_proof_max_age_days: int = int(
        os.environ.get("ADDRESS_PROOF_MAX_AGE_DAYS", "90")
    )

    # Persistence files (JSON store)
    data_dir: str = os.environ.get("DATA_DIR", "data")
    audit_log_file: str = os.environ.get(
        "AUDIT_LOG_FILE", "data/audit_log.jsonl"
    )
    cases_db_file: str = os.environ.get("CASES_DB_FILE", "data/cases.json")

    host: str = os.environ.get("HOST", "0.0.0.0")
    port: int = int(os.environ.get("PORT", "8000"))


settings = Settings()

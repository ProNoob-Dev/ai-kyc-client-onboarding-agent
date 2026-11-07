"""KYC requirements retrieval.

The knowledge base is ``data/requirements.json`` — the onboarding
requirement rules (name consistency, registration consistency, document
expiry, address-proof recency, required documents, ...). Retrieval is a
deterministic in-memory vector search (hash-based bag-of-words vectors
with cosine similarity), so the app always runs with no external
service. The validation agent uses the same ``search()`` interface
either way.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass

_DIM = 128


def _embed(text: str) -> list[float]:
    """Deterministic bag-of-words embedding (no model call needed)."""
    vec = [0.0] * _DIM
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        idx = int(hashlib.md5(token.encode()).hexdigest(), 16) % _DIM
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


@dataclass
class RuleHit:
    rule: dict
    score: float


class RequirementsKB:
    """Keyword + vector hybrid over the local requirements file."""

    def __init__(self, rules: list[dict]) -> None:
        self._rules = rules
        self._vectors = [
            _embed(r["title"] + " " + r["text"] + " " + " ".join(r.get("keywords", [])))
            for r in rules
        ]

    def search(self, query: str, top_k: int = 4) -> list[RuleHit]:
        q = _embed(query)
        scored: list[RuleHit] = []
        for rule, vec in zip(self._rules, self._vectors):
            score = _cosine(q, vec)
            # Small keyword bonus keeps exact terms on top.
            kw_hits = sum(
                1 for kw in rule.get("keywords", []) if kw.lower() in query.lower()
            )
            score += 0.05 * kw_hits
            scored.append(RuleHit(rule=rule, score=round(score, 4)))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]


def load_rules(data_dir: str | None = None) -> list[dict]:
    base = data_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
    )
    path = os.path.join(base, "requirements.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build_requirements_kb(data_dir: str | None = None) -> RequirementsKB:
    return RequirementsKB(load_rules(data_dir))

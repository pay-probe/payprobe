"""Error-text normalization — the privacy and clustering front door.

Every error message entering the corpus passes through :func:`normalize`
BEFORE storage, so (a) clusters form on failure *shape* rather than instance
noise (hosts, ports, ids, timings), and (b) any digit sequence — including a
PAN that leaked into an error string — is masked out and never persisted
(ADR-0005 §5 "what the model never sees").
"""
from __future__ import annotations

import hashlib
import re

_RULES: list[tuple[re.Pattern, str]] = [
    # UUIDs before generic hex/number masking.
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    # host:port and ip[:port]
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b"), "<host>"),
    (re.compile(r"\b[a-zA-Z][\w.-]*:\d{2,5}\b"), "<host>"),
    # hex blobs, long ids
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<hex>"),
    # every remaining digit run (durations, codes, PANs, sequence numbers)
    (re.compile(r"\d+"), "<n>"),
    # collapse whitespace
    (re.compile(r"\s+"), " "),
]


def normalize(text: str | None) -> str:
    """Mask instance-specific tokens; lowercase; collapse whitespace."""
    if not text:
        return ""
    out = str(text)
    for pattern, repl in _RULES:
        out = pattern.sub(repl, out)
    return out.strip().lower()


def signature(text: str | None) -> str:
    """Stable id of a failure *shape*: sha1 of the normalized text."""
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:16]

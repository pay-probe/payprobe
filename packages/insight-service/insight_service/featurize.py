"""Structured feature channels — the context the error string doesn't say.

Derives low-cardinality tokens from a failed step's surroundings (target,
action, response code, HTTP status class, duration bucket, environment,
failed-assertion fields) and joins them into a single space-separated string.
Underscored tokens (``tgt_hsm``, ``rc_05``) survive TF-IDF tokenization as
single vocabulary entries, so each channel contributes its own dimension.

Design invariants (see docs/insight-feature-analysis.md §1.1):

* channels live in the ``feats`` column, NEVER inside the normalized text —
  the failure ``signature`` stays a hash of the pure error text, so dedup and
  bulk corrections are unaffected;
* a plain-text inference (no context) still works — missing tokens are
  simply absent from the vector;
* every token is masked-safe by construction: values are slugged and only
  short identifiers are kept (no free text enters a channel).
"""
from __future__ import annotations

import re
from typing import Any

_SLUG = re.compile(r"[^a-z0-9]+")
#: digits transliterated to letters so tokens survive normalize()'s digit
#: masking identically at train and predict time (rc "05" → "rc_af", "5xx" →
#: "fxx") — deterministic, cardinality-preserving, and normalize-proof.
_DIGITS = str.maketrans("0123456789", "abcdefghij")


def _slug(value: Any, max_len: int = 24) -> str:
    s = _SLUG.sub("_", str(value).strip().lower()).strip("_")
    return s.translate(_DIGITS)[:max_len]


def _dur_bucket(ms: Any) -> str | None:
    try:
        v = float(ms)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v < 100:
        return "fast"
    if v < 1000:
        return "mid"
    if v < 5000:
        return "slow"
    return "veryslow"


def _response_code(response: Any) -> str | None:
    """Fish the protocol result code out of a response payload: ISO 8583
    DE39 (``response_code``), generic ``rc``/``status``, or an HTTP body's
    nested ``response_code``. Only short alphanumerics qualify — this is a
    channel, not a free-text leak."""
    if not isinstance(response, dict):
        return None
    for container in (response, response.get("body")
                      if isinstance(response.get("body"), dict) else {}):
        for key in ("response_code", "rc"):
            v = container.get(key)
            if v is not None and len(str(v)) <= 6:
                return _slug(v, 6)
    return None


def _http_class(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    try:
        sc = int(response.get("status_code"))
    except (TypeError, ValueError):
        return None
    return f"{sc // 100}xx" if 100 <= sc <= 599 else None


def context_tokens(step: dict | None = None, environment: str = "",
                   extra: dict | None = None) -> str:
    """Channel tokens for one failed step (or an /infer context dict).

    ``step``: a run-summary step (target/action/response/duration_ms/
    assertions). ``extra``: explicit overrides, e.g. the predict step's
    optional context params — same keys, caller-supplied."""
    step = step or {}
    extra = extra or {}
    toks: list[str] = []

    def add(prefix: str, value: Any) -> None:
        if value:
            toks.append(f"{prefix}_{_slug(value)}")

    add("tgt", extra.get("target") or step.get("target"))
    add("act", extra.get("action") or step.get("action"))

    rc = extra.get("response_code")
    rc = _slug(rc, 6) if rc is not None and str(rc).strip() else \
        _response_code(step.get("response"))
    if rc:
        toks.append(f"rc_{rc}")

    http = _http_class(step.get("response"))
    if http:
        toks.append(f"http_{http.translate(_DIGITS)}")

    add("env", extra.get("environment") or environment)

    dur = _dur_bucket(extra.get("duration_ms") or step.get("duration_ms"))
    if dur:
        toks.append(f"dur_{dur}")

    # which assertion fields failed — names only, never expected/actual values
    for a in (step.get("assertions") or [])[:5]:
        if isinstance(a, dict) and not a.get("passed", True):
            add("asrt", a.get("field") or a.get("name"))

    if extra.get("source"):
        add("src", extra["source"])

    return " ".join(dict.fromkeys(toks))  # dedupe, keep order


def training_text(feats: str, norm: str) -> str:
    """What the text models actually train/predict on."""
    return f"{feats} {norm}".strip()

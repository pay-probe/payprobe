"""What happened to a failed load transaction: the outcome taxonomy.

``errors`` on a load run counts every transaction that did not succeed. This
module splits that count by *what went wrong on the wire*, so the operator can
tell a host that answered "no" from a host that did not answer at all:

``rejected``
    A reply came back, but it was not accepted: a rejecting response code, a
    failed assertion, an HTTP error status, an HSM error code.
``timeout``
    No reply arrived within the adapter's response timeout.
``connection``
    The connection was refused, lost while waiting, or never came back.
``other``
    Anything else: a payload that could not be encoded, a scenario error,
    an exception with no recognisable cause.

The keys are fixed (no per-message cardinality), so they add across the fleet
and compare across runs. Classification uses structure first (did the failed
step carry a reply?) and the error text only when there is no reply; the text
patterns are the strings the adapters in this repo actually produce.
"""

from __future__ import annotations

import re
from typing import Any

REJECTED = "rejected"
TIMEOUT = "timeout"
CONNECTION = "connection"
OTHER = "other"

#: every outcome, in display order. A driver reports all four (zeros included)
#: so a reader can tell "no timeouts" from "a run recorded before this existed".
OUTCOMES: tuple[str, ...] = (REJECTED, TIMEOUT, CONNECTION, OTHER)

# A lost or never-established connection. Checked before the timeout patterns:
# "Connection lost: [Errno 60] Operation timed out" is a lost connection, and a
# soak client's "connect: TimeoutError" never got as far as sending.
_CONN_FIRST = re.compile(
    r"^(re)?connect:|connection lost|not connected|did not reconnect|hsm connection closed"
)
# No reply within the response timeout. TCP: "No response within 30s."; HTTP:
# "request timed out after 5000 ms"; HSM / NATS / thrown: "TimeoutError".
_TIMEOUT = re.compile(r"no response within|no reply within|timed out|timeout|deadline exceeded")
_CONN = re.compile(
    r"refused|connect call failed|connection reset|reset by peer|broken pipe|peer closed|"
    r"connecterror|"
    r"connectionerror|connection closed|unreachable|no route to host|incompleteread|"
    r"noresponders|no responders"
)


def classify_error_text(error: str | None) -> str:
    """The outcome for a failure known only by its error text."""
    e = (error or "").strip().lower()
    if not e:
        return OTHER
    if _CONN_FIRST.search(e):
        return CONNECTION
    if _TIMEOUT.search(e):
        return TIMEOUT
    if _CONN.search(e):
        return CONNECTION
    return OTHER


def classify_exception(exc: BaseException) -> str:
    """The outcome for an exception thrown out of a transaction."""
    if isinstance(exc, TimeoutError):
        return TIMEOUT
    if isinstance(exc, (ConnectionError, OSError)):
        return CONNECTION
    return classify_error_text(f"{type(exc).__name__}: {exc}")


def classify_step(response: Any, error: str | None) -> str:
    """The outcome for a failed step: a step that carried a reply was
    rejected; one without a reply is classified by its error text."""
    if response:
        return REJECTED
    return classify_error_text(error)


def empty_outcomes() -> dict[str, int]:
    return {k: 0 for k in OUTCOMES}

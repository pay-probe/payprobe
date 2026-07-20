"""Reusable MCP prompts — domain workflows a client can surface as slash
commands / templates. Each returns a user-message string that steers the agent
through the right PayProbe tools in the right order.

Kept SDK-free; ``server.py`` registers each with ``FastMCP.prompt``.
"""
from __future__ import annotations


def author_scenario(intent: str, environment: str = "mock") -> str:
    """Author and save a PayProbe scenario from a plain-English intent."""
    return (
        f"Author a PayProbe test scenario for: {intent!r}.\n\n"
        "Work in this order:\n"
        "1. Call `list_step_catalog` to see the available targets/actions, and "
        "`list_connections` / `list_environments` for valid endpoints.\n"
        "2. Call `assist_scenario` with the intent to get a grounded draft flow.\n"
        "3. Call `validate_scenario` on the draft and fix any reported issues.\n"
        "4. Show me the final flow and, once I confirm, call `create_scenario`.\n"
        f"5. Offer to `run_scenario` against the {environment!r} environment.\n\n"
        "Only use targets/actions that actually appear in the catalog."
    )


def diagnose_failed_run(run_id: str) -> str:
    """Triage a failed run and propose fixes."""
    return (
        f"Diagnose PayProbe run {run_id!r}.\n\n"
        "1. Call `diagnose_run` to get the failure classification and likely causes.\n"
        "2. Call `get_run` to read the per-step request/response for the failing steps.\n"
        "3. Call `compare_runs` to see whether this is a regression from a prior run.\n"
        "Then summarise: what failed, why, whether it's a regression, and the "
        "smallest change that would fix it. Don't change anything without asking."
    )


def decode_iso8583(message: str) -> str:
    """Decode and explain a raw ISO 8583 wire message."""
    return (
        "Decode this ISO 8583 message and explain it field by field "
        f"(MTI, bitmap, each DE, and any EMV TLV in DE 55):\n\n{message}\n\n"
        "Use `iso8583_analyze`. Flag anything that fails validation."
    )


def regression_triage(days: int = 30) -> str:
    """Review regression health and surface unstable tests."""
    return (
        f"Review PayProbe regression health over the last {days} days.\n\n"
        "1. Call `run_trend` for the per-day pass-rate.\n"
        "2. Call `run_flakiness` to surface scenarios that intermittently pass/fail.\n"
        "Summarise the trend direction, call out the flakiest scenarios, and "
        "recommend which to stabilise first."
    )


# (function, title, description) — server.py registers these.
PROMPT_SPECS = [
    (author_scenario, "Author a scenario",
     "Draft, validate and save a scenario from a plain-English intent."),
    (diagnose_failed_run, "Diagnose a failed run",
     "Triage a failed run, check for regression, and propose a fix."),
    (decode_iso8583, "Decode an ISO 8583 message",
     "Decode a raw wire message field by field."),
    (regression_triage, "Regression triage",
     "Review pass-rate trend and surface flaky scenarios."),
]

"""Hub-wide quotas (ADR-0010 phase 5).

A definition's ``budget.daily_tokens`` bounds one agent. These bound the hub:
whatever the registry holds, the operator has one place to cap what a day of
agents may cost and how many heartbeats may run at once. All three are plain
environment knobs read once at startup (``/health.quotas`` shows the effective
values); ``0`` switches a quota off.

- ``AGENT_HUB_DAILY_TOKENS`` (default 5,000,000): tokens all agents together
  may spend per UTC day. A wake past the ceiling is refused and recorded as
  ``budget_exceeded`` (the same schedulability stop as the per-agent budget,
  so it alerts the same way).
- ``AGENT_HUB_MAX_CONCURRENT`` (default 4): heartbeats that may be ``running``
  at once across all agents. A wake past the cap is refused and recorded as
  ``quota_exceeded``; a schedule trigger simply fires again on its next due
  tick, an event wake is lost and says so in the row. Per agent the cap is
  already one (coalescing, D7).
- ``AGENT_LOAD_APPROVAL_TPS`` (default 100): a ``start_load_run`` above this
  ``target_tps`` (or ``end_tps`` / ``spike_tps``) from inside a heartbeat is
  refused by the tool layer; only a workflow ``tool`` node, which must sit
  behind an ``approval`` outside mock, may start heavier load. Enforced in
  ``payprobe_common.agent_toolkit.scoped_dispatch`` (invariant #6), fed from
  here by the runner.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_DAILY_TOKENS = 5_000_000
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_LOAD_APPROVAL_TPS = 100


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Quotas:
    daily_tokens: int = DEFAULT_DAILY_TOKENS
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    load_approval_tps: int = DEFAULT_LOAD_APPROVAL_TPS

    @classmethod
    def from_env(cls) -> Quotas:
        return cls(
            daily_tokens=_int_env("AGENT_HUB_DAILY_TOKENS", DEFAULT_DAILY_TOKENS),
            max_concurrent=_int_env("AGENT_HUB_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT),
            load_approval_tps=_int_env("AGENT_LOAD_APPROVAL_TPS", DEFAULT_LOAD_APPROVAL_TPS),
        )

    def as_dict(self) -> dict:
        return {
            "daily_tokens": self.daily_tokens or None,
            "max_concurrent": self.max_concurrent or None,
            "load_approval_tps": self.load_approval_tps or None,
        }

    def tokens_refusal(self, spent_today: int) -> str | None:
        """The refusal message when the hub-wide ceiling is spent, else None."""
        if self.daily_tokens and spent_today >= self.daily_tokens:
            return (
                f"hub-wide daily token ceiling ({self.daily_tokens}) spent "
                f"({spent_today} used today; AGENT_HUB_DAILY_TOKENS); hard stop"
            )
        return None

    def concurrency_refusal(self, running: int) -> str | None:
        """The refusal message when too many heartbeats run at once, else None."""
        if self.max_concurrent and running >= self.max_concurrent:
            return (
                f"{running} heartbeats already running "
                f"(AGENT_HUB_MAX_CONCURRENT={self.max_concurrent}); wake refused"
            )
        return None


def load_approval_tps() -> int | None:
    """The tool-layer cap the runner hands to :class:`ToolScope` (None = off)."""
    return _int_env("AGENT_LOAD_APPROVAL_TPS", DEFAULT_LOAD_APPROVAL_TPS) or None

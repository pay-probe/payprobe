"""
MockAdapter — in-memory adapter for local development and CI.
Returns configurable canned responses. No real system required.
"""
import asyncio
import time
from ..base.base_adapter import BaseAdapter, StepResult

DEFAULT_RESPONSES = {
    "send_auth_request": {"response_code": "00", "auth_code": "123456", "rrn": "000000000001"},
    "insert_card":       {"status": "inserted"},
    "enter_pin":         {"status": "accepted"},
    "tap_nfc":           {"status": "tapped"},
    "query_transaction": {"status": "APPROVED", "amount": 10000},
    "verify_pin_block":  {"verified": True},
    "query_account":     {"status": "ACTIVE", "balance": 500000},
    "send_0100":         {"mti": "0110", "response_code": "00"},
}


class MockAdapter(BaseAdapter):

    async def connect(self) -> None:
        self._latency_ms = self.config.get("latency_ms", 5)

    async def health_check(self) -> bool:
        await asyncio.sleep(self._latency_ms / 1000)
        return not self.config.get("force_unhealthy", False)

    async def execute(self, action: str, payload: dict) -> StepResult:
        start = time.monotonic()
        await asyncio.sleep(self._latency_ms / 1000)
        overrides = self.config.get("responses", {})
        response = overrides.get(action, DEFAULT_RESPONSES.get(action, {"status": "ok"}))
        duration = int((time.monotonic() - start) * 1000)
        return StepResult(
            success=True,
            request_payload=payload,
            response_payload=response,
            duration_ms=duration,
        )

    async def disconnect(self) -> None:
        pass

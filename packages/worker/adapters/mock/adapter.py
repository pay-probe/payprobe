"""
MockAdapter — in-memory adapter for local development and CI.
Returns configurable canned responses. No real system required.
"""

import asyncio
import time
from ..base.base_adapter import BaseAdapter, StepResult

DEFAULT_RESPONSES = {
    "send_auth_request": {"response_code": "00", "auth_code": "123456", "rrn": "000000000001"},
    "insert_card": {"status": "inserted"},
    "enter_pin": {"status": "accepted"},
    "remove_card": {"status": "removed"},
    "tap_nfc": {"status": "tapped"},
    "query_transaction": {"status": "APPROVED", "amount": 10000},
    "verify_pin_block": {"verified": True},
    "query_account": {"status": "ACTIVE", "balance": 500000},
    # RestPay (http) keepalive + payment lifecycle actions. echo_test carries
    # both "status" and "response_code" so it satisfies the example heartbeat
    # scenario (asserts response_code) and the switch_settlement pack (asserts
    # status) — the same action is checked both ways.
    "echo_test": {"status": "ok", "response_code": "00", "echo": "alive"},
    "send_refund": {"response_code": "00", "auth_code": "654321", "rrn": "000000000002"},
    "send_reversal": {"response_code": "00", "rrn": "000000000003"},
    # ISO 8583 message-type actions (purchase 0200/0210, reversal 0420/0430).
    "send_0100": {"mti": "0110", "response_code": "00"},
    "send_0200": {"mti": "0210", "response_code": "00", "rrn": "000000000010"},
    "send_0420": {"mti": "0430", "response_code": "00"},
    # payShield HSM host-command actions (payshield_hsm_smoke example). Mock mode
    # can't run the HSM crypto, so return canned payShield-shaped replies carrying
    # the fields that scenario's assertions check (error_code / firmware / ok /
    # key / cvv). The real HSMAdapter + simulator compute these for real in the
    # payshield-sim env; this only lets the smoke pass under `mode: mock` in CI.
    "nc": {"error_code": "00", "firmware": "0007-E000", "lmk_check": "0000000000000000"},
    "generate_key": {
        "error_code": "00",
        "ok": True,
        "key": "U0000000000000000000000000000000000",
        "kcv": "000000",
    },
    "generate_cvv": {"error_code": "00", "cvv": "000"},
    "verify_cvv": {"error_code": "00"},
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
            raw_log=(
                f"[mock] → {action}\n"
                f"  REQ {payload}\n"
                f"[mock] ← canned reply\n"
                f"  RES {response}"
            ),
        )

    async def disconnect(self) -> None:
        pass

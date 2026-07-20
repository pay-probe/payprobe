"""PayProbe BaseAdapter — all adapters extend this class."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AssertionResult:
    field: str
    expected: Any
    actual: Any
    operator: str
    passed: bool


@dataclass
class StepResult:
    success: bool
    request_payload: Any
    response_payload: Any
    duration_ms: int
    assertions: list = field(default_factory=list)
    raw_log: str = ""
    error: str | None = None

    @property
    def failed(self) -> bool:
        return not self.success or not all(a.passed for a in self.assertions)


class BaseAdapter(ABC):

    def __init__(self, config: dict, pool_size: int = 100):
        self.config = config
        self.pool_size = pool_size
        self.session_context: dict = {}

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection / warm up pool."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Phase 1 component check. Never raise — return False on error."""

    @abstractmethod
    async def execute(self, action: str, payload: dict) -> StepResult:
        """Execute one step. Must be fully async."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Release all resources."""

    def assert_response(self, response: dict, assertions: list[dict]) -> list:
        results = []
        for rule in assertions:
            actual = self._extract_field(response, rule["field"])
            passed = self._evaluate(actual, rule["operator"], rule.get("expected"))
            results.append(
                AssertionResult(
                    field=rule["field"],
                    expected=rule.get("expected"),
                    actual=actual,
                    operator=rule["operator"],
                    passed=passed,
                )
            )
        return results

    def _extract_field(self, data: dict, path: str) -> Any:
        current = data
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current

    def _evaluate(self, actual: Any, operator: str, expected: Any) -> bool:
        try:
            ops = {
                "eq": lambda a, e: a == e,
                "ne": lambda a, e: a != e,
                "lt": lambda a, e: a < e,
                "gt": lambda a, e: a > e,
                "lte": lambda a, e: a <= e,
                "gte": lambda a, e: a >= e,
                "present": lambda a, e: a is not None,
                "absent": lambda a, e: a is None,
                "contains": lambda a, e: e in str(a),
            }
            return ops[operator](actual, expected)
        except (TypeError, KeyError):
            return False

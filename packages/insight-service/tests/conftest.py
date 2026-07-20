"""Pytest wiring — the actual fixtures/helpers live in ``insight_fixtures``.

NOTE: no ``__init__.py`` in this directory, deliberately. With one, this suite
becomes package ``tests`` — the same name scenario-service's suite resolves to
(their hyphenated parent dirs can't be packages) — and cross-package collection
dies with ImportPathMismatchError. The helper module carries a unique basename
(``insight_fixtures``) for the same reason. Mirrors payprobe-assistant/tests.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make importable however the suite is invoked (repo rule: from packages/
# with PYTHONPATH=packages): `insight_service` (package parent), `report_service`
# (packages/), `insight_fixtures` (this dir — needed because without
# __init__.py pytest only inserts the test file's own dirname).
_HERE = Path(__file__).resolve().parent
for p in (str(_HERE.parents[1]), str(_HERE.parents[0]), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("PAYPROBE_ENV", "test")

import pytest  # noqa: E402

from insight_fixtures import build_history  # noqa: E402

from insight_service.ingest import Ingestor  # noqa: E402
from insight_service.rest import OrchestratorReader  # noqa: E402
from insight_service.store import InsightStore  # noqa: E402


@pytest.fixture()
def history() -> list[dict]:
    return build_history()


@pytest.fixture()
def store() -> InsightStore:
    return InsightStore(":memory:")


@pytest.fixture()
def ingestor(store, history) -> Ingestor:
    from insight_fixtures import FakeOrchestrator

    fake = FakeOrchestrator(history)
    reader = OrchestratorReader(base_url="http://fake", fetch=fake.fetch)
    return Ingestor(store, reader)

"""JSON extraction from real model output (bare, fenced, or inside a report)."""

import json

from agent_hub.alerts import events_for, extract_json, findings_of
from agent_hub.engine import _json_maybe

# The observer's first real (scheduled, Haiku) wake on 2026-09-23 answered with
# a markdown report: heading, fenced json, prose summary. Bare-JSON parsing
# would have dropped two critical findings on the floor.
REPORT = """## Platform Status Report - 2026-09-23T13:23:04Z

```json
[
  {"severity": "critical", "subject": "scn-785a1f59", "headline": "25 consecutive failures"},
  {"severity": "critical", "subject": "scn-110037dc", "headline": "93% failure prediction"},
  {"severity": "warn", "subject": "showcase-net", "headline": "live but idle"},
  {"severity": "info", "subject": "platform health", "headline": "all services healthy"}
]
```

**Summary:** two standing regressions, no dead services.
"""


def test_extract_json_finds_the_fenced_block_inside_a_report():
    data = extract_json(REPORT)
    assert isinstance(data, list) and len(data) == 4
    assert data[0]["severity"] == "critical"
    assert _json_maybe(REPORT) == data  # the engine sees the same thing


def test_extract_json_bare_fenced_embedded_and_garbage():
    assert extract_json('{"verdict": "approve"}') == {"verdict": "approve"}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json("```\n[1, 2]\n```") == [1, 2]
    assert extract_json('Verdict below.\n{"verdict": "changes_requested"}\nThanks.') == {
        "verdict": "changes_requested"
    }
    assert extract_json("no json here") is None
    assert extract_json("") is None
    assert extract_json(None) is None
    assert extract_json("42") is None  # a scalar is not a result document
    assert extract_json("[not, json]") is None


def test_findings_from_a_report_reach_the_alerter():
    hb = {"id": "h", "agent": "observer", "version": 1, "status": "done", "result": REPORT}
    events = events_for(hb, mode="advisor")
    assert [p["finding"]["subject"] for _, p in events] == [
        "scn-785a1f59",
        "scn-110037dc",
        "showcase-net",
    ]  # warn and above; the info line stays out
    assert len(findings_of(REPORT)) == 4
    assert findings_of(json.dumps({"findings": findings_of(REPORT)[:1]})) == findings_of(REPORT)[:1]

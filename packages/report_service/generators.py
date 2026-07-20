"""Report generators — pure functions over a run-detail dict.

These were previously inlined in the orchestrator. They own no state and touch
no network or DB: each takes a ``detail`` (or ``cert``) dict and returns a
string/dict. Keeping them here gives reporting a single tested home and lets the
orchestrator (or a future standalone report service) delegate without
duplicating format logic.

* :func:`junit_xml`        — JUnit XML for CI ingestion.
* :func:`html_report`      — self-contained printable HTML run report.
* :func:`certify`          — score a run against a test-case pack.
* :func:`certification_html` — HTML certification badge + table.
* :func:`index_steps`      — {(scenario, step): status} for run-to-run diffs.
* :func:`run_rollup`       — aggregate stats (counts, failures, slowest steps).
"""
from __future__ import annotations

import json
from datetime import datetime
from html import escape
from xml.sax.saxutils import quoteattr
from typing import Any


# -- shared helpers -----------------------------------------------------------

def _fmt_ms(ms: Any) -> str:
    """42 → '42 ms', 1234 → '1.23 s', 65000 → '1m 05s'."""
    try:
        ms = int(ms or 0)
    except (TypeError, ValueError):
        return "—"
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.2f} s"
    m, s = divmod(round(ms / 1000), 60)
    return f"{m}m {s:02d}s"


def _parse_iso(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _wall_ms(detail: dict) -> int | None:
    """Wall-clock run duration from started/completed timestamps, if present."""
    a = _parse_iso(detail.get("started_at"))
    b = _parse_iso(detail.get("completed_at") or detail.get("finished_at"))
    if a and b and b >= a:
        return int((b - a).total_seconds() * 1000)
    return None


def _preview(value: Any, limit: int = 1200) -> str:
    """Compact, truncated text preview of a payload for embedding in HTML."""
    if value is None or value == "" or value == {} or value == []:
        return ""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str, indent=2)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    if len(text) > limit:
        text = text[:limit] + f"\n… (+{len(text) - limit} more chars)"
    return text


def run_rollup(detail: dict) -> dict:
    """Aggregate stats over a run detail — one pass, reused by every report.

    Returns scenario/step/assertion counts, total step time, wall duration,
    the list of failing steps (for triage) and the slowest steps (for tuning).
    """
    scenarios = (detail.get("summary") or {}).get("scenarios", []) or []
    sc_counts = {"total": len(scenarios), "passed": 0, "failed": 0, "blocked": 0}
    st_counts = {"total": 0, "passed": 0, "failed": 0, "error": 0, "blocked": 0}
    as_counts = {"total": 0, "passed": 0}
    step_ms = 0
    failures: list[dict] = []
    timed: list[dict] = []
    for sc in scenarios:
        st = sc.get("status")
        if st in sc_counts:
            sc_counts[st] += 1
        name = sc.get("name") or sc.get("scenario_id") or "scenario"
        for s in sc.get("steps", []) or []:
            st_counts["total"] += 1
            sst = s.get("status")
            if sst in st_counts:
                st_counts[sst] += 1
            ms = int(s.get("duration_ms") or 0)
            step_ms += ms
            row = {
                "scenario": str(name), "step_id": str(s.get("step_id", "")),
                "target": f'{s.get("target", "")}.{s.get("action", "")}',
                "status": sst, "duration_ms": ms, "error": s.get("error"),
            }
            timed.append(row)
            if sst in ("failed", "error"):
                failures.append(row)
            for a in s.get("assertions") or []:
                as_counts["total"] += 1
                if a.get("passed"):
                    as_counts["passed"] += 1
    graded = st_counts["passed"] + st_counts["failed"] + st_counts["error"]
    return {
        "scenarios": sc_counts,
        "steps": st_counts,
        "assertions": as_counts,
        "step_ms": step_ms,
        "wall_ms": _wall_ms(detail),
        "failures": failures,
        "slowest": sorted(timed, key=lambda r: -r["duration_ms"])[:5],
        "pass_rate": round(st_counts["passed"] / graded * 100, 1) if graded else 0.0,
    }


# -- certification -----------------------------------------------------------

def score_cases(run_detail: dict, pack: dict) -> list[dict]:
    """Map each pack case to the matching scenario's outcome.

    Shared scoring used by both :func:`certify` and the gate evaluator
    (``report_service.gates``). Carries ``priority`` through from the pack case
    so gates can apply per-tier thresholds.
    """
    by_name = {
        s.get("name"): s.get("status")
        for s in (run_detail.get("summary") or {}).get("scenarios", [])
    }
    cases: list[dict] = []
    for c in pack.get("cases", []):
        name = (c.get("scenario") or {}).get("name") or c.get("id")
        status = by_name.get(name, "not_run")
        cases.append({
            "id": c.get("id"), "requirement": c.get("requirement", ""),
            "scenario": name, "status": status, "passed": status == "passed",
            "priority": c.get("priority"),
        })
    return cases


def certify(run_detail: dict, pack: dict) -> dict:
    """Score a run against a pack's cases -> certification summary.

    Besides the overall compliance number this now breaks the score down per
    priority tier (``by_priority``) and names the failing / never-run cases so
    a reader knows *what* to fix, not just *that* something failed.
    """
    cases = score_cases(run_detail, pack)
    passed = sum(1 for c in cases if c["passed"])
    total = len(cases)
    by_priority: dict[str, dict] = {}
    for c in cases:
        tier = str(c.get("priority") or "unspecified")
        b = by_priority.setdefault(tier, {"passed": 0, "total": 0})
        b["total"] += 1
        b["passed"] += 1 if c["passed"] else 0
    for b in by_priority.values():
        b["pct"] = round(b["passed"] / b["total"] * 100, 1) if b["total"] else 0.0
    return {
        "run_id": run_detail.get("id"), "pack": pack.get("id"),
        "scheme": pack.get("scheme"), "label": pack.get("label"),
        "environment": run_detail.get("environment"),
        "run_completed_at": run_detail.get("completed_at"),
        "cases": cases, "passed": passed, "total": total,
        "compliance_pct": round(passed / total * 100, 1) if total else 0.0,
        "certified": total > 0 and passed == total,
        "by_priority": by_priority,
        "failed_cases": [c["id"] for c in cases
                         if not c["passed"] and c["status"] != "not_run"],
        "not_run_cases": [c["id"] for c in cases if c["status"] == "not_run"],
    }


def certification_html(cert: dict) -> str:
    rows = "".join(
        f"<tr class='{'p' if c['passed'] else ('n' if c['status']=='not_run' else 'f')}'>"
        f"<td>{escape(str(c['id']))}</td><td>{escape(c['requirement'])}</td>"
        f"<td>{escape(str(c.get('scenario') or '—'))}</td>"
        f"<td>{escape(str(c.get('priority') or '—'))}</td>"
        f"<td>{escape(c['status'])}</td></tr>"
        for c in cert["cases"]
    )
    badge = "CERTIFIED" if cert["certified"] else "NOT CERTIFIED"
    color = "#2ea043" if cert["certified"] else "#f85149"
    pct = cert["compliance_pct"]
    tier_chips = "".join(
        f"<span class='chip {'ok' if b.get('passed') == b.get('total') else 'ko'}'>"
        f"{escape(str(t))}: {b.get('passed')}/{b.get('total')}"
        f" ({b.get('pct', 0)}%)</span>"
        for t, b in sorted((cert.get("by_priority") or {}).items())
    )
    failed = cert.get("failed_cases") or []
    not_run = cert.get("not_run_cases") or []
    gaps = ""
    if failed or not_run:
        parts = []
        if failed:
            parts.append(f"<b>Failed:</b> {escape(', '.join(map(str, failed)))}")
        if not_run:
            parts.append(f"<b>Not run:</b> {escape(', '.join(map(str, not_run)))}")
        gaps = f"<p class='gaps'>{' &middot; '.join(parts)}</p>"
    return f"""<!doctype html><html><head><meta charset='utf-8'>
<title>Certification — {escape(str(cert['label']))}</title>
<style>body{{font-family:system-ui,sans-serif;margin:32px;color:#111}}
h1{{font-size:20px}} .badge{{display:inline-block;padding:4px 12px;border-radius:999px;
color:#fff;background:{color};font-weight:700;font-size:13px}}
table{{border-collapse:collapse;margin-top:16px;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:8px;text-align:left;font-size:14px}}
tr.p td:last-child{{color:#2ea043}} tr.f td:last-child{{color:#f85149}}
tr.n td:last-child{{color:#999}} .meta{{color:#666;font-size:13px}}
.bar{{height:8px;border-radius:4px;background:#eee;margin:10px 0;max-width:420px}}
.bar>i{{display:block;height:100%;border-radius:4px;background:{color};width:{pct}%}}
.chip{{display:inline-block;border-radius:999px;padding:2px 10px;font-size:12px;
margin:2px 6px 2px 0;border:1px solid #ddd}}
.chip.ok{{color:#1a7f37;border-color:#a6d9b0}}.chip.ko{{color:#cf222e;border-color:#f0b6bb}}
.gaps{{font-size:13px;color:#444;background:#fff8f7;border:1px solid #f0dcda;
border-radius:8px;padding:8px 12px}}</style></head>
<body>
<h1>{escape(str(cert['label']))} <span class='badge'>{badge}</span></h1>
<p class='meta'>Scheme: {escape(str(cert['scheme']))} · Pack: {escape(str(cert['pack']))}
 · Compliance: <b>{cert['compliance_pct']}%</b> ({cert['passed']}/{cert['total']})
 · Run: {escape(str(cert['run_id']))}
 · Environment: {escape(str(cert.get('environment') or '—'))}
 · Completed: {escape(str(cert.get('run_completed_at') or '—'))}</p>
<div class='bar'><i></i></div>
<div>{tier_chips}</div>
{gaps}
<table><thead><tr><th>Case</th><th>Requirement</th><th>Scenario</th>
<th>Priority</th><th>Result</th></tr></thead>
<tbody>{rows}</tbody></table>
</body></html>"""


# -- Go/No-Go sign-off document ---------------------------------------------

SIGNOFF_CSS = (
    "@page{size:A4;margin:18mm}"
    "*{box-sizing:border-box}"
    "body{font:13px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:#1a1a1a;"
    "margin:0;background:#fff}"
    ".doc{max-width:760px;margin:0 auto;padding:24px}"
    "h1{font-size:19px;margin:0 0 2px}h2{font-size:14px;margin:22px 0 8px;"
    "border-bottom:1px solid #e3e3e3;padding-bottom:4px}"
    ".sub{color:#666;font-size:12px;margin:0 0 18px}"
    ".verdict{display:flex;align-items:center;gap:16px;border:2px solid #ccc;"
    "border-radius:10px;padding:14px 18px;margin:16px 0}"
    ".verdict .b{font-size:26px;font-weight:800;letter-spacing:.04em;"
    "padding:6px 18px;border-radius:8px;color:#fff}"
    ".go{border-color:#1a7f37;background:#eaf6ec}.go .b{background:#1a7f37}"
    ".nogo{border-color:#cf222e;background:#fbeaec}.nogo .b{background:#cf222e}"
    ".vmeta{font-size:12px;color:#444}"
    "table{border-collapse:collapse;width:100%;font-size:12px}"
    "td,th{border-bottom:1px solid #eee;padding:6px 8px;text-align:left;"
    "vertical-align:top}"
    "th{color:#666;font-weight:600}"
    ".ok{color:#1a7f37;font-weight:700}.bad{color:#cf222e;font-weight:700}"
    ".mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}"
    ".kv{display:grid;grid-template-columns:150px 1fr;gap:4px 14px;font-size:12px}"
    ".kv dt{color:#666}.kv dd{margin:0;word-break:break-all}"
    ".hash{background:#f6f8fa;border:1px solid #e3e3e3;border-radius:6px;"
    "padding:6px 8px;font-size:10px}"
    ".stats{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0}"
    ".stat{border:1px solid #e3e3e3;border-radius:8px;padding:6px 12px;"
    "font-size:12px;text-align:center}"
    ".stat b{display:block;font-size:16px}"
    ".foot{margin-top:26px;color:#888;font-size:10px;border-top:1px solid #eee;"
    "padding-top:8px}"
)


def signoff_html(snapshot: dict) -> str:
    """A printable, self-contained Go/No-Go sign-off document (ADR-0003).

    Verdict-first and light-themed for attaching to a change ticket or audit
    (browser / headless-Chrome ``Print → PDF`` produces the PDF artifact). Takes
    an immutable sign-off snapshot: verdict, gate result, provenance, content
    hash, and the approval trail — plus, when the snapshot carries the frozen
    run ``summary``, an evidence section with the scenario-level outcomes.
    """
    gr = snapshot.get("gate_result") or {}
    prov = snapshot.get("provenance") or {}
    verdict = snapshot.get("verdict", "NO_GO")
    is_go = verdict == "GO"
    cls = "go" if is_go else "nogo"
    label = "GO" if is_go else "NO-GO"

    gates = gr.get("gates", [])
    gates_ok = sum(1 for g in gates if g.get("ok"))
    gate_rows = "".join(
        f"<tr><td class='{'ok' if g.get('ok') else 'bad'}'>"
        f"{'PASS' if g.get('ok') else 'FAIL'}</td>"
        f"<td class='mono'>{escape(str(g.get('id', '')))}</td>"
        f"<td>{escape(str(g.get('detail', '')))}</td></tr>"
        for g in gates
    ) or "<tr><td colspan='3'>No gates evaluated.</td></tr>"

    pk = prov.get("pack") or {}
    sut = prov.get("system_under_test") or {}
    eps = prov.get("endpoints") or []
    ep_html = "".join(
        f"<div class='mono'>{escape(str(e.get('target', '')))}: "
        f"{escape(str(e.get('host', '')))}:{escape(str(e.get('port', '')))}</div>"
        for e in eps
    ) or "<span style='color:#888'>none recorded</span>"

    # ADR-0007: ad-hoc Playground fires that hit a live simulator during this
    # run's window inflate its stats/traces — surface it so a reader can weigh
    # the contamination rather than trust clean-looking numbers.
    pg = prov.get("playground_traffic") or {}
    pg_total = int(pg.get("total") or 0)
    if pg_total:
        by_sim = pg.get("by_simulator") or {}
        detail = ", ".join(
            f"{escape(str(sid))}: {int(n)}" for sid, n in by_sim.items()
        )
        pg_html = (
            f"<span class='bad'>{pg_total}</span> ad-hoc playground message(s) hit "
            f"a running simulator during this run window — its stats/traces are "
            f"contaminated ({escape(detail)})."
        )
    else:
        pg_html = "<span style='color:#888'>none — no playground traffic in the run window</span>"

    approvals = snapshot.get("approvals") or []
    appr_rows = "".join(
        f"<tr><td>{escape(str(a.get('by', '')))}</td>"
        f"<td>{escape(str(a.get('decision', '')))}</td>"
        f"<td>{escape(str(a.get('at', '')))}</td>"
        f"<td>{escape(str(a.get('note', '')))}</td></tr>"
        for a in approvals
    ) or "<tr><td colspan='4' style='color:#888'>No approvals recorded.</td></tr>"

    # Evidence: scenario-level outcomes from the frozen run summary, if the
    # snapshot carries one. A sign-off reader shouldn't need to open the run.
    evidence = ""
    scenarios = (snapshot.get("summary") or {}).get("scenarios") or []
    if scenarios:
        counts = {"passed": 0, "failed": 0, "blocked": 0}
        rows = []
        for sc in scenarios:
            st = sc.get("status", "?")
            if st in counts:
                counts[st] += 1
            steps = sc.get("steps") or []
            ms = sum(int(s.get("duration_ms") or 0) for s in steps)
            first_err = next(
                (s.get("error") for s in steps
                 if s.get("status") in ("failed", "error") and s.get("error")), "")
            rows.append(
                f"<tr><td class='{'ok' if st == 'passed' else ('bad' if st in ('failed', 'error') else '')}'>"
                f"{escape(str(st))}</td>"
                f"<td>{escape(str(sc.get('name') or sc.get('scenario_id') or '—'))}</td>"
                f"<td>{len(steps)}</td><td>{escape(_fmt_ms(ms))}</td>"
                f"<td>{escape(str(first_err or '')[:160])}</td></tr>"
            )
        evidence = (
            "<h2>Evidence — scenario outcomes</h2>"
            "<div class='stats'>"
            f"<div class='stat'><b>{len(scenarios)}</b>scenarios</div>"
            f"<div class='stat'><b class='ok'>{counts['passed']}</b>passed</div>"
            f"<div class='stat'><b class='bad'>{counts['failed']}</b>failed</div>"
            f"<div class='stat'><b>{counts['blocked']}</b>blocked</div></div>"
            "<table><thead><tr><th>Result</th><th>Scenario</th><th>Steps</th>"
            "<th>Duration</th><th>First error</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    return f"""<!doctype html><html><head><meta charset='utf-8'>
<title>Sign-off {escape(str(snapshot.get('id', '')))}</title>
<style>{SIGNOFF_CSS}</style></head><body><div class='doc'>
<h1>PayProbe Production Sign-off</h1>
<p class='sub'>Pack <b>{escape(str(snapshot.get('pack') or '—'))}</b> ·
 environment <b>{escape(str(snapshot.get('environment') or '—'))}</b> ·
 run <span class='mono'>{escape(str(snapshot.get('run_id', '')))}</span> ·
 sign-off <span class='mono'>{escape(str(snapshot.get('id', '')))}</span></p>

<div class='verdict {cls}'><span class='b'>{label}</span>
<div class='vmeta'>Certified {escape(str(snapshot.get('created_at') or '—'))}
 by {escape(str(snapshot.get('created_by') or '—'))}<br>
 {gates_ok}/{len(gates)} gate(s) passed ·
 {len(gr.get('blocking') or [])} blocking gate(s)</div></div>

<h2>Gates</h2>
<table><thead><tr><th>Result</th><th>Gate</th><th>Detail</th></tr></thead>
<tbody>{gate_rows}</tbody></table>
{evidence}
<h2>Provenance</h2>
<dl class='kv'>
<dt>Environment</dt><dd>{escape(str(prov.get('environment') or '—'))}</dd>
<dt>Pack</dt><dd>{escape(str(pk.get('id') or '—'))} {escape(str(pk.get('version') or ''))}</dd>
<dt>System under test</dt><dd class='mono'>{escape(str(sut.get('build') or '—'))}
 ({escape(str(sut.get('source') or '—'))})</dd>
<dt>Triggered by</dt><dd>{escape(str(prov.get('triggered_by') or '—'))}</dd>
<dt>Baseline run</dt><dd class='mono'>{escape(str(prov.get('baseline_run_id') or 'none'))}</dd>
<dt>Started</dt><dd>{escape(str(prov.get('started_at') or '—'))}</dd>
<dt>Completed</dt><dd>{escape(str(prov.get('completed_at') or '—'))}</dd>
<dt>Endpoints</dt><dd>{ep_html}</dd>
<dt>Playground traffic</dt><dd>{pg_html}</dd>
</dl>

<h2>Approval trail</h2>
<table><thead><tr><th>By</th><th>Decision</th><th>At</th><th>Note</th></tr></thead>
<tbody>{appr_rows}</tbody></table>

<h2>Integrity</h2>
<div class='hash'>{escape(str(snapshot.get('content_hash') or '—'))}</div>
<p class='foot'>Tamper-evident: this hash covers the run summary, gate verdict and
 provenance. Any edit to the frozen artifact changes it. Generated by PayProbe.</p>
</div></body></html>"""


# -- JUnit + diff ------------------------------------------------------------

def junit_xml(detail: dict) -> str:
    """Render a run summary as JUnit XML (one <testsuite> per scenario, one
    <testcase> per step) for CI ingestion.

    Emits the standard ``skipped``/``time``/``timestamp`` attributes so CI
    dashboards get durations and skip counts, and attaches assertion detail
    for failed steps as ``<system-out>`` so the diff is readable in CI without
    opening PayProbe.
    """
    summary = detail.get("summary") or {}
    scenarios = summary.get("scenarios", []) or []
    ts_attr = ""
    started = detail.get("started_at")
    if started:
        ts_attr = f" timestamp={quoteattr(str(started))}"
    suites: list[str] = []
    t_tests = t_fail = t_err = t_skip = 0
    t_secs = 0.0
    for sc in scenarios:
        steps = sc.get("steps", []) or []
        name = sc.get("name") or sc.get("scenario_id") or "scenario"
        fails = sum(1 for s in steps if s.get("status") == "failed")
        errs = sum(1 for s in steps if s.get("status") == "error")
        skips = sum(1 for s in steps if s.get("status") == "blocked")
        secs_total = sum((s.get("duration_ms", 0) or 0) for s in steps) / 1000
        t_tests += len(steps)
        t_fail += fails
        t_err += errs
        t_skip += skips
        t_secs += secs_total
        cases: list[str] = []
        for s in steps:
            sid = s.get("step_id", "step")
            secs = (s.get("duration_ms", 0) or 0) / 1000
            body = ""
            if s.get("status") == "failed":
                body = f"<failure message={quoteattr(s.get('error') or 'assertion failed')}/>"
            elif s.get("status") == "error":
                body = f"<error message={quoteattr(s.get('error') or 'error')}/>"
            elif s.get("status") == "blocked":
                body = "<skipped/>"
            if s.get("status") in ("failed", "error"):
                lines = [
                    f"target: {s.get('target', '')}.{s.get('action', '')}",
                    *(
                        f"assert {a.get('field')} {a.get('operator')} "
                        f"{a.get('expected')!r} -> {a.get('actual')!r} "
                        f"[{'ok' if a.get('passed') else 'FAIL'}]"
                        for a in (s.get("assertions") or [])
                    ),
                ]
                body += f"<system-out>{escape(chr(10).join(lines))}</system-out>"
            cases.append(
                f'    <testcase classname={quoteattr(str(name))} '
                f'name={quoteattr(str(sid))} time="{secs:.3f}">{body}</testcase>'
            )
        suites.append(
            f'  <testsuite name={quoteattr(str(name))} tests="{len(steps)}" '
            f'failures="{fails}" errors="{errs}" skipped="{skips}" '
            f'time="{secs_total:.3f}"{ts_attr}>\n' + "\n".join(cases) + "\n  </testsuite>"
        )
    head = (
        f'<testsuites name={quoteattr(detail.get("label") or detail.get("id", "run"))} '
        f'tests="{t_tests}" failures="{t_fail}" errors="{t_err}" '
        f'skipped="{t_skip}" time="{t_secs:.3f}">'
    )
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + head + "\n" + "\n".join(suites) + "\n</testsuites>\n"


def index_steps(summary: dict | None) -> dict[tuple[str, str], str]:
    """{(scenario_name, step_id): status} for diffing two runs."""
    out: dict[tuple[str, str], str] = {}
    for sc in (summary or {}).get("scenarios", []) or []:
        name = sc.get("name") or sc.get("scenario_id") or "?"
        for st in sc.get("steps", []) or []:
            out[(str(name), str(st.get("step_id")))] = st.get("status", "?")
    return out


# -- HTML run report ---------------------------------------------------------

HTML_CSS = (
    "body{font:14px/1.5 system-ui,sans-serif;margin:0;background:#0d1117;color:#e6edf3;padding:24px}"
    "h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 8px;color:#adbac7}"
    ".meta{color:#8b949e;font-size:13px;margin-bottom:18px}"
    ".cards{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 6px}"
    ".card{background:#161b22;border:1px solid #30363d;border-radius:10px;"
    "padding:8px 16px;min-width:96px;text-align:center}"
    ".card b{display:block;font-size:19px}.card span{font-size:11px;color:#8b949e;"
    "text-transform:uppercase;letter-spacing:.04em}"
    ".card .ok{color:#3fb950}.card .ko{color:#ff7b72}.card .bl{color:#d29922}"
    ".bar{height:8px;border-radius:4px;background:#21262d;margin:8px 0 18px;max-width:520px;overflow:hidden}"
    ".bar>i{display:block;height:100%;background:#3fb950}"
    "table.t{border-collapse:collapse;width:100%;font-size:13px;margin:0 0 6px}"
    "table.t td,table.t th{border-bottom:1px solid #21262d;padding:5px 8px;text-align:left}"
    "table.t th{color:#8b949e;font-weight:600;font-size:12px}"
    ".sc{border:1px solid #30363d;border-radius:10px;margin:0 0 14px;overflow:hidden}"
    ".sc>.h{display:flex;gap:10px;align-items:center;padding:10px 14px;background:#161b22}"
    ".sc>.h .nm{font-weight:600}.sc>.h .sm{margin-left:auto;color:#768390;font-size:12px}"
    ".st{padding:8px 14px;border-top:1px solid #21262d;font-size:13px}"
    ".st .r{display:flex;gap:10px;align-items:center}"
    ".id{font-family:ui-monospace,monospace;color:#79c0ff;min-width:90px}"
    ".tg{flex:1;color:#adbac7}.ms{color:#768390}"
    ".err{color:#ff7b72;margin:5px 0 0}"
    ".b{padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;text-transform:uppercase}"
    ".b.passed{background:#1a3326;color:#3fb950}.b.failed,.b.error{background:#3a1d1d;color:#ff7b72}"
    ".b.blocked,.b.pending,.b.running,.b.partial{background:#2d2a1a;color:#d29922}"
    ".b.env{background:#1c2a3a;color:#79c0ff;text-transform:none}"
    "table.as{border-collapse:collapse;margin:6px 0 0;font-size:12px;width:100%}"
    "table.as td,table.as th{border-top:1px solid #21262d;padding:2px 6px;text-align:left}"
    "table.as .x{color:#ff7b72}"
    "details.pl{margin:5px 0 0}details.pl summary{cursor:pointer;color:#8b949e;"
    "font-size:12px;user-select:none}"
    "details.pl pre{background:#161b22;border:1px solid #30363d;border-radius:8px;"
    "padding:8px 10px;font-size:11px;overflow:auto;max-height:280px;margin:4px 0 0;"
    "white-space:pre-wrap;word-break:break-all}"
    ".foot{margin-top:22px;color:#768390;font-size:11px;border-top:1px solid #21262d;padding-top:8px}"
)


def _details_block(label: str, value: Any) -> str:
    text = _preview(value)
    if not text:
        return ""
    return (
        f'<details class="pl"><summary>{escape(label)}</summary>'
        f"<pre>{escape(text)}</pre></details>"
    )


def html_report(detail: dict) -> str:
    """A self-contained, printable HTML run report (no external assets).

    Leads with the numbers a reader wants first — scenario/step/assertion
    counts, pass rate, durations, phase results and a failure-triage table —
    then the full per-step breakdown with assertions, request/response
    payloads and wire logs tucked into collapsible blocks.
    """
    summary = detail.get("summary") or {}
    scenarios = summary.get("scenarios", []) or []
    title = detail.get("label") or detail.get("id", "run")
    roll = run_rollup(detail)
    sc, st, asr = roll["scenarios"], roll["steps"], roll["assertions"]

    dur_bits = [f"steps {_fmt_ms(roll['step_ms'])}"]
    if roll["wall_ms"] is not None:
        dur_bits.insert(0, f"wall {_fmt_ms(roll['wall_ms'])}")
    head = (
        f"<h1>PayProbe Run Report — {escape(str(title))}</h1>"
        f'<div class="meta">run {escape(detail.get("id", ""))} · '
        f'status <b>{escape(detail.get("status", "?"))}</b> · '
        f'env {escape(str(detail.get("environment") or "—"))} · '
        f'started {escape(str(detail.get("started_at") or "—"))} · '
        f'completed {escape(str(detail.get("completed_at") or "—"))} · '
        f'duration {escape(", ".join(dur_bits))}</div>'
        '<div class="cards">'
        f'<div class="card"><b>{sc["total"]}</b><span>scenarios</span></div>'
        f'<div class="card"><b class="ok">{sc["passed"]}</b><span>passed</span></div>'
        f'<div class="card"><b class="ko">{sc["failed"]}</b><span>failed</span></div>'
        f'<div class="card"><b class="bl">{sc["blocked"]}</b><span>blocked</span></div>'
        f'<div class="card"><b>{st["total"]}</b><span>steps</span></div>'
        f'<div class="card"><b class="{"ko" if st["failed"] + st["error"] else "ok"}">'
        f'{st["failed"] + st["error"]}</b><span>step fails</span></div>'
        f'<div class="card"><b>{asr["passed"]}/{asr["total"]}</b><span>assertions</span></div>'
        f'<div class="card"><b>{roll["pass_rate"]}%</b><span>pass rate</span></div>'
        "</div>"
        f'<div class="bar"><i style="width:{roll["pass_rate"]}%"></i></div>'
    )

    # Phase results (component health, integration, e2e) when present.
    phases = summary.get("phases") or {}
    phase_html = ""
    if phases:
        rows = []
        for key in sorted(phases):
            p = phases[key] or {}
            pst = str(p.get("status", "?"))
            if "components" in p:
                bits = [f'{p.get("components", 0)} component(s) probed']
                if p.get("failed"):
                    bits.append(f'failed: {", ".join(map(str, p["failed"]))}')
                if p.get("recovered"):
                    bits.append(
                        f'recovered on retry: {", ".join(map(str, p["recovered"]))} ⚠ flaky')
                note = " · ".join(bits)
            else:
                note = " · ".join(
                    f"{p.get(k, 0)} {k}" for k in ("passed", "failed", "blocked") if k in p
                ) or "—"
            rows.append(
                f'<tr><td>{escape(key.replace("_", " "))}</td>'
                f'<td><span class="b {escape(pst)}">{escape(pst)}</span></td>'
                f"<td>{escape(note)}</td></tr>"
            )
        phase_html = (
            "<h2>Phases</h2><table class='t'><thead><tr><th>Phase</th>"
            "<th>Status</th><th>Detail</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    # Failure triage — every failing step in one table, before the detail.
    fail_html = ""
    if roll["failures"]:
        frows = "".join(
            f"<tr><td>{escape(f['scenario'])}</td>"
            f"<td class='id'>{escape(f['step_id'])}</td>"
            f"<td>{escape(f['target'])}</td>"
            f"<td><span class='b {escape(str(f['status']))}'>{escape(str(f['status']))}</span></td>"
            f"<td>{escape(_fmt_ms(f['duration_ms']))}</td>"
            f"<td class='err'>{escape(str(f['error'] or '')[:300])}</td></tr>"
            for f in roll["failures"]
        )
        fail_html = (
            f"<h2>Failure triage — {len(roll['failures'])} failing step(s)</h2>"
            "<table class='t'><thead><tr><th>Scenario</th><th>Step</th><th>Target</th>"
            "<th>Status</th><th>Duration</th><th>Error</th></tr></thead>"
            f"<tbody>{frows}</tbody></table>"
        )

    # Slowest steps — quick latency hot-spot check.
    slow_html = ""
    if st["total"] > 1:
        srows = "".join(
            f"<tr><td class='id'>{escape(r['step_id'])}</td>"
            f"<td>{escape(r['scenario'])}</td><td>{escape(r['target'])}</td>"
            f"<td>{escape(_fmt_ms(r['duration_ms']))}</td></tr>"
            for r in roll["slowest"]
        )
        slow_html = (
            "<h2>Slowest steps</h2><table class='t'><thead><tr><th>Step</th>"
            "<th>Scenario</th><th>Target</th><th>Duration</th></tr></thead>"
            f"<tbody>{srows}</tbody></table>"
        )

    body: list[str] = []
    for scn in scenarios:
        name = escape(str(scn.get("name") or scn.get("scenario_id") or "scenario"))
        stt = escape(str(scn.get("status", "?")))
        steps = scn.get("steps", []) or []
        sc_ms = sum(int(s.get("duration_ms") or 0) for s in steps)
        rows: list[str] = []
        for s in steps:
            sid = escape(str(s.get("step_id", "")))
            tgt = escape(f'{s.get("target", "")}.{s.get("action", "")}')
            sst = escape(str(s.get("status", "?")))
            ms = s.get("duration_ms", 0) or 0
            env_b = (
                f'<span class="b env">@{escape(str(s.get("environment_override")))}</span>'
                if s.get("environment_override") else ""
            )
            err = (
                f'<div class="err">{escape(str(s.get("error")))}</div>'
                if s.get("error") else ""
            )
            asserts = ""
            arows = s.get("assertions") or []
            if arows:
                trs = "".join(
                    f'<tr><td>{escape(str(a.get("field", "")))}</td>'
                    f'<td>{escape(str(a.get("operator", "")))}</td>'
                    f'<td>{escape(str(a.get("expected", "")))}</td>'
                    f'<td>{escape(str(a.get("actual", "")))}</td>'
                    f'<td class="{"" if a.get("passed") else "x"}">'
                    f'{"✓" if a.get("passed") else "✗"}</td></tr>'
                    for a in arows
                )
                asserts = (
                    '<table class="as"><tr><th>field</th><th>op</th><th>expected</th>'
                    f"<th>actual</th><th></th>{trs}</table>"
                )
            payloads = (
                _details_block("request", s.get("request"))
                + _details_block("response", s.get("response"))
                + _details_block("wire log", s.get("raw_log"))
            )
            rows.append(
                f'<div class="st"><div class="r"><span class="id">{sid}</span>'
                f'<span class="tg">{tgt}</span>{env_b}<span class="ms">{ms}ms</span>'
                f'<span class="b {sst}">{sst}</span></div>{err}{asserts}{payloads}</div>'
            )
        body.append(
            f'<div class="sc"><div class="h"><span class="b {stt}">{stt}</span>'
            f'<span class="nm">{name}</span>'
            f'<span class="sm">{len(steps)} step(s) · {escape(_fmt_ms(sc_ms))}</span>'
            f'</div>{"".join(rows)}</div>'
        )
    detail_html = "<h2>Scenario detail</h2>" if body else ""
    foot = (
        f'<div class="foot">Generated by PayProbe · run '
        f'{escape(detail.get("id", ""))} · {sc["total"]} scenario(s), '
        f'{st["total"]} step(s), {asr["total"]} assertion(s)</div>'
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Run {escape(str(title))}</title><style>{HTML_CSS}</style></head>"
        f"<body>{head}{phase_html}{fail_html}{slow_html}{detail_html}"
        f"{''.join(body)}{foot}</body></html>"
    )

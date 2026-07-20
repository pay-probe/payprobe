# PayProbe Report Service

The report **generators** (JUnit XML, self-contained HTML run report,
certification scoring + badge, and run-to-run step diffs) are implemented as a
reusable, tested library at **`packages/report_service/`** and imported by the
orchestrator (which owns the run data and exposes the report endpoints:
`/runs/{id}/junit`, `/runs/{id}/html`, `/runs/{id}/certification[/html]`).

Keeping the format logic in a standalone library means it can be lifted into a
dedicated HTTP service later without touching the rendering code. This directory
remains the placeholder for that future deployable service; the compose
`report-service` container currently runs the shared stub app.

```bash
cd ../report_service && pip install -e ".[dev]" && pytest tests/ -v
```

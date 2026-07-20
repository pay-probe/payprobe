# PayProbe Insight Service

Read-only advisory service over run history (ADR-0005): failure
categorization (heuristic taxonomy + optional learned clustering), evidence-
pack failure explanations, and run-outcome / flakiness prediction with a
self-scoring frequency baseline.

**The model advises; the registry and the gates decide.** This service has no
write access to registries, runs, or verdicts.

```
RUN_API_URL=http://localhost:8100 uvicorn insight_service.main:app --port 8500
```

- `GET  /status` — health + model versions + Brier self-score
- `POST /train` — incremental ingest + refit the learned categorizer
- `GET  /insights/failures/{run_id}` — categorized failures + explanations
- `GET  /insights/categories` — taxonomy with counts
- `GET  /insights/predictions[?environment=]` — per-scenario failure/flakiness probabilities
- `POST /insights/categories/{id}/rename` — name a discovered cluster

The learned layer needs the optional `ml` extra (`pip install ".[ml]"`);
without it, everything runs on the deterministic baselines. Design:
`docs/architecture/insight-service.md`.

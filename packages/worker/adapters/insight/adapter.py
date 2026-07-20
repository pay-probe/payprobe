"""InsightAdapter — the scenario `predict` step (ADR-0005 extension).

Lets a scenario ask the insight service for a model opinion mid-flow and
branch on the answer::

    [call insight.categorize {text: "${auth_step.response.error}"}]
        → ${predict.response.category} / .confidence / .novel
    [call insight.predict_outcome {scenario_id: "sc-42"}]
        → ${predict.response.p_fail_next} / .p_flaky / .basis

Downstream ``if``/``switch`` nodes decide what to do with it — that is the
scenario author's control flow. The boundary stays intact: this output feeds
YOUR test logic, never the report gates or sign-off.

Config (a Connection with ``adapter: "insight"``)::

    {"adapter": "insight",
     "base_url": "http://insight-service:8500",   # default: $INSIGHT_API_URL
     "token": "..."}                               # optional static bearer;
                                                   # else AUTH_JWT_SECRET mint

Stdlib urllib only — no optional deps, so the adapter always registers.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from ..base.base_adapter import BaseAdapter, StepResult

ACTIONS = ("categorize", "explain", "predict_outcome", "model_status", "train")


class InsightAdapter(BaseAdapter):
    async def connect(self) -> None:
        self.base_url = str(
            self.config.get("base_url")
            or os.environ.get("INSIGHT_API_URL", "http://localhost:8500")
        ).rstrip("/")
        self.token = str(self.config.get("token") or "")
        self.timeout = float(self.config.get("response_timeout_sec") or 10)

    async def health_check(self) -> bool:
        try:
            self._get("/health")
            return True
        except Exception:  # noqa: BLE001 — health checks never raise
            return False

    async def execute(self, action: str, payload: dict) -> StepResult:
        started = time.monotonic()
        payload = payload or {}
        try:
            if action == "model_status":
                # pre-flight assert: which models are live before relying on
                # them (e.g. assert ${step.response.custom_model_active})
                st = self._get("/status")
                cat = st.get("categorizer") or {}
                data = {
                    "corpus_size": st.get("corpus_size", 0),
                    "custom_model_active": bool(cat.get("custom")),
                    "custom_model": (cat.get("custom") or {}).get("name"),
                    "cluster_model_active": bool(cat.get("learned")),
                    "predictor_learned": bool((st.get("predictor") or {}).get("learned")),
                    "sklearn_available": bool(st.get("sklearn_available")),
                }
                request: Any = {"action": "model_status"}
                log_line = f"GET {self.base_url}/status"
            elif action == "train":
                # pipeline step: ingest new runs + refit after a data load
                data = self._post("/train", {})
                request = {"action": "train"}
                log_line = f"POST {self.base_url}/train"
            else:
                request = self._infer_body(action, payload)
                data = self._post("/infer", request)
                log_line = f"POST {self.base_url}/infer"
            return StepResult(
                success=True,
                request_payload=request,
                response_payload=data,
                duration_ms=int((time.monotonic() - started) * 1000),
                raw_log=f"{log_line} -> {json.dumps(data)[:500]}",
            )
        except (ValueError, RuntimeError) as exc:
            return StepResult(
                success=False,
                request_payload=payload,
                response_payload=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=str(exc),
            )

    async def disconnect(self) -> None:  # stateless HTTP — nothing to close
        return None

    # -- internals -------------------------------------------------------

    def _infer_body(self, action: str, payload: dict) -> dict:
        if action in ("categorize", "explain"):
            text = str(payload.get("text") or "").strip()
            if not text:
                raise ValueError(
                    f"{action} needs a `text` param — usually a reference "
                    "like ${some_step.response.error}"
                )
            body: dict[str, Any] = {"kind": action, "text": text}
            if payload.get("model_id"):
                # pin one registered model instead of the active chain
                body["model_id"] = str(payload["model_id"])
            # structured context → feature-channel tokens server-side, so
            # inference sees the same channels training did
            ctx = {
                k: payload[k]
                for k in ("target", "action", "response_code", "environment", "duration_ms")
                if payload.get(k) not in (None, "")
            }
            if ctx:
                body["context"] = ctx
            return body
        if action == "predict_outcome":
            sid = str(payload.get("scenario_id") or "").strip()
            if not sid:
                raise ValueError("predict_outcome needs a `scenario_id` param")
            body: dict[str, Any] = {"kind": "predict_outcome", "scenario_id": sid}
            if payload.get("environment"):
                body["environment"] = str(payload["environment"])
            return body
        raise ValueError(f"unknown insight action '{action}' — use one of {ACTIONS}")

    def _auth_header(self) -> dict[str, str]:
        token = self.token or os.environ.get("API_TOKEN") or ""
        if token:
            return {"Authorization": f"Bearer {token}"}
        secret = os.environ.get("AUTH_JWT_SECRET")
        if secret:
            try:
                import jwt

                now = int(time.time())
                minted = jwt.encode(
                    {"sub": "worker-insight", "iat": now, "exp": now + 300},
                    secret,
                    algorithm="HS256",
                )
                return {"Authorization": f"Bearer {minted}"}
            except ImportError:
                pass
        return {}

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **self._auth_header()},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"insight service HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(
                f"insight service unreachable at {self.base_url} ({exc}) — "
                "it is an optional deployment; start it (:8500) or remove "
                "this predict step"
            )

    def _get(self, path: str) -> Any:
        return self._request("GET", path)

    def _post(self, path: str, body: dict) -> Any:
        return self._request("POST", path, body)

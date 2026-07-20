/** Production: same-origin via nginx. `/api/scenarios/`→scenario-service,
 *  `/api/orch/`→run-orchestrator (REST + WebSocket). The WS URL is derived
 *  from runApiBase (relative -> absolute, http(s)->ws(s)) at runtime. */
export const environment = {
  production: true,
  scenarioApiBase: "/api/scenarios",
  runApiBase: "/api/orch",
  authApiBase: "/api/auth",
  // Where the chat is sent: the standalone payprobe-assistant via nginx
  // (/api/assistant/* → assistant:8400). It is the LLM egress boundary.
  // Provider config: ASSIST_LLM_* env wins; without it the service pulls the
  // Settings → AI assistant config from scenario-service over the service-
  // gated /assist/config/material (2026-07-13). Cutover 2026-07-07 (ATLAS #5).
  assistantApiBase: "/api/assistant",
  // Advisory ML insight service (ADR-0005) — failure categorization,
  // explanations, outcome predictions. Advise-only; the portal degrades
  // gracefully when it is not deployed.
  insightApiBase: "/api/insights",
};

/** Development: talk to locally running services. */
export const environment = {
  production: false,
  scenarioApiBase: "http://localhost:8000",
  // run-orchestrator base (REST). The WebSocket URL is derived from this by
  // swapping http(s)->ws(s), so REST and the live stream always agree.
  runApiBase: "http://localhost:8100",
  // auth-service base (JWT issuance + /me).
  authApiBase: "http://localhost:8300",
  // Where the chat is sent: the standalone payprobe-assistant (the LLM egress
  // boundary). Provider config: ASSIST_LLM_* env wins; without it the service
  // pulls the Settings → AI assistant config from scenario-service over the
  // service-gated /assist/config/material (2026-07-13). Cutover 2026-07-07
  // (ATLAS roadmap #5).
  // To fall back to the in-process shim during transition, override the
  // "assistant" endpoint in Settings → Endpoints to localhost:8000.
  assistantApiBase: "http://localhost:8400",
  // Advisory ML insight service (ADR-0005) — failure categorization,
  // explanations, outcome predictions. Advise-only; the portal degrades
  // gracefully when it is not deployed.
  insightApiBase: "http://localhost:8500",
};

# Security Policy

PayProbe is a payment-systems **testing** tool. It handles the kind of material
real payment systems handle — PANs, PIN blocks, cryptographic keys, HSM
commands — so it is built to keep that material safe, and reports about where it
doesn't are genuinely welcome.

## What PayProbe is (and is not)

PayProbe is source-available software you run yourself. It ships with **mock
defaults** and **test keys** (for example a test LMK for the payShield
simulator, and `dev-insecure-change-me` style secrets in the compose files) so
it runs with nothing real out of the box. Those defaults are for local use
only — they are not a vulnerability, and the portal's **Settings → System**
panel flags when you are still running them.

The project is not affiliated with any payment network, vendor, or employer,
and contains no proprietary specifications, production data, or real
credentials (see the disclaimer in the [README](README.md)).

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report privately using GitHub's **"Report a vulnerability"** button under the
repository's **Security** tab (Security → Advisories → Report a vulnerability).
That opens a private advisory only the maintainers can see.

Please include:

- what the issue is and which component it affects (worker, orchestrator,
  scenario-service, auth-service, mcp-server, assistant, insight-service,
  portal, or a specific adapter/simulator);
- steps to reproduce, or a proof of concept;
- the impact you think it has;
- the version / commit you tested.

You will get an acknowledgement as soon as a maintainer sees it. As a
noncommercial hobby project maintained in spare time there is no paid bounty and
no guaranteed SLA, but real issues are taken seriously and fixed as a priority,
and reporters are credited in the advisory unless they ask not to be.

## Scope

In scope — things worth reporting:

- secret material (keys, PINs, PANs, tokens) that leaks in an API response, a
  log, a report, a capture, or a file on disk when it should be masked or
  encrypted;
- authentication or RBAC bypass (the fail-closed JWT gate, the last-admin
  guard, service-token verification);
- a way for a sandboxed **code step** to escape its network namespace or reach
  something it shouldn't;
- injection, SSRF, or path traversal in any service endpoint;
- the assistant or MCP surface performing a write or execute it should not be
  able to, or exposing secrets through a tool result.

Out of scope — expected behaviour, not bugs:

- the shipped mock/test defaults being insecure (they are meant to be replaced
  in production — see above);
- issues that require an already-compromised host or root on the machine
  running the stack;
- the proxy/tap, chaos injection, and load generation doing exactly what they
  are designed to do (they are offensive-by-purpose testing tools — use them
  only against systems you own or are authorized to test).

## Handling secrets safely

If you deploy PayProbe beyond local mock use:

- set a real `AUTH_JWT_SECRET`, `POSTGRES_PASSWORD`, and admin password —
  never the compose placeholders;
- set `PAYPROBE_SECRET_KEY` so secret-named fields (connection credentials,
  variables, keys, NATS auth, the assistant LLM key) are encrypted at rest with
  `SecretBox` rather than stored as plaintext JSON;
- keep credentials in the scoped **variables/secrets** mechanism or an external
  secret manager, never in a connection doc or a committed scenario;
- run the code-step sandbox in `strict` mode (`PAYPROBE_CODE_SANDBOX=strict`)
  if you accept code steps from anyone you don't fully trust.

See the [configuration reference](docs/operations/configuration.md) for every
security-relevant environment variable and its default.

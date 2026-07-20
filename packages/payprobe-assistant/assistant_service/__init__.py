"""PayProbe configuration assistant service.

A standalone, tool-calling agent and unified LLM gateway. It reads and writes
PayProbe configuration by calling the scenario-service / orchestrator REST APIs
(authenticated with a service JWT), runs a provider-neutral tool-calling loop,
and journals every write so changes are reversible. It is the only service that
talks to LLM providers (single egress boundary, single key store).
"""

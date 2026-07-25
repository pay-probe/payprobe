"""General config assistant — the multi-turn, tool-calling agent loop.

The loop is **provider-neutral**: it keeps the conversation in a small internal
format (roles ``system`` / ``user`` / ``assistant`` / ``tool``) and delegates one
model turn to a *caller* — :func:`provider_tool_call` in production, a scripted
stub in tests. Each turn the model either requests tool calls (dispatched
through :mod:`api.agent_tools`, results fed back) or returns a final answer.

This is the prototype that will move into the standalone ``payprobe-assistant``
service; keeping the loop free of FastAPI/store specifics (everything arrives via
:class:`~api.agent_tools.ToolContext` and ``llm``) is what makes that cheap.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .agent_tools import ToolContext, dispatch, schemas_for
from .assist import _http_post_json, _strip_fences

#: Hard cap on tool-call rounds so a confused model can't loop forever.
MAX_ITERATIONS = 8

SYSTEM_PROMPT = (
    "You are PayProbe's configuration assistant. You help the user set up and "
    "manage adapters/connections, message formats, data tables, variables, "
    "starter flows, scenarios and networks by calling the provided tools. "
    "You can also SEE runtime state (read-only): platform_status, list_runs, "
    "list_network_runs, list_running_participants, list_running_simulators, "
    "list_load_runs — use these for 'what is running / is it healthy' "
    "questions. You can also START a load test yourself: start_load_run "
    "(scenario_ids or inline scenarios + a profile) returns a run_id, then "
    "poll get_load_run(run_id) until status is 'completed' to report the "
    "metrics. Apart from load tests you never start or stop runtime pieces "
    "(listeners, simulators, networks, scenario runs) — that stays with the "
    "operator.\n"
    "Rules:\n"
    "- READ BEFORE YOU WRITE. Inspect current state with the list_/get_ tools "
    "before creating or changing anything, and prefer editing what exists over "
    "recreating it.\n"
    "- Use ONLY the provided tools to make changes; never claim a change you did "
    "not perform with a tool.\n"
    "- Writes apply immediately but are reversible — explain what you changed.\n"
    "- If a tool returns an error or is blocked by a guardrail, explain it to the "
    "user instead of retrying blindly.\n"
    "- PORTAL GEOGRAPHY (when telling the user where to click): scenarios are "
    "built and RUN from the Scenarios editor's Run button, and re-run from the "
    "Runs page; /load is load testing; /model-studio is ML training only "
    "(datasets + custom categorizer models — scenarios never appear there); "
    "networks are authored and started on the Networks canvas. Never invent "
    "pages or buttons — if unsure where something lives, say what the API/tool "
    "does instead.\n"
    "- Be concise."
)


# -- provider-neutral loop ----------------------------------------------------

CallerResult = dict  # {"text": str, "tool_calls": [{"id","name","args"}]}
Caller = Callable[[list[dict], list[dict], dict], CallerResult]


def iter_agent(messages: list[dict], ctx: ToolContext, llm: dict,
               *, tiers: tuple[str, ...] = ("read", "write", "execute"),
               max_iterations: int = MAX_ITERATIONS,
               caller: Caller | None = None):
    """Run the loop, *yielding progress events* as they happen.

    Events (all dicts with a ``type``):
    * ``{"type":"tool","phase":"start","tool":name}`` — about to run a tool
    * ``{"type":"tool","phase":"end","tool":name,"ok":bool,"guardrail":bool,
      "error":str|None}`` — tool finished
    * ``{"type":"final", reply, tool_calls, journal, iterations, stopped}`` —
      the model's answer (always emitted exactly once, last)

    Tool *args* are deliberately omitted from the stream (they may carry
    secrets); only the tool name is surfaced as live status."""
    call = caller or provider_tool_call
    tools = schemas_for(tiers)
    convo: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}, *messages]
    executed: list[dict] = []

    for i in range(1, max_iterations + 1):
        resp = call(convo, tools, llm)
        calls = resp.get("tool_calls") or []
        if not calls:
            yield {"type": "final", "reply": resp.get("text", ""),
                   "tool_calls": executed, "journal": ctx.journal.entries(),
                   "iterations": i, "stopped": False}
            return
        convo.append({"role": "assistant", "content": resp.get("text", ""),
                      "tool_calls": calls})
        for tc in calls:
            yield {"type": "tool", "phase": "start", "tool": tc["name"]}
            out = dispatch(ctx, tc["name"], tc.get("args") or {})
            executed.append(out)
            yield {"type": "tool", "phase": "end", "tool": tc["name"],
                   "ok": out.get("ok", False), "guardrail": out.get("guardrail", False),
                   "error": out.get("error")}
            convo.append({"role": "tool", "tool_call_id": tc.get("id", tc["name"]),
                          "name": tc["name"], "content": json.dumps(out)})

    yield {"type": "final",
           "reply": "Stopped after the maximum number of tool steps. Ask me to "
                    "continue if there's more to do.",
           "tool_calls": executed, "journal": ctx.journal.entries(),
           "iterations": max_iterations, "stopped": True}


def run_agent(messages: list[dict], ctx: ToolContext, llm: dict,
              *, tiers: tuple[str, ...] = ("read", "write", "execute"),
              max_iterations: int = MAX_ITERATIONS,
              caller: Caller | None = None) -> dict:
    """Run the agent to completion (non-streaming). Returns the final ``reply``
    plus the ``tool_calls`` executed, the session ``journal`` entries, and
    ``iterations`` — the same contract as before, now built on :func:`iter_agent`."""
    final: dict = {}
    for ev in iter_agent(messages, ctx, llm, tiers=tiers,
                         max_iterations=max_iterations, caller=caller):
        if ev.get("type") == "final":
            final = {k: v for k, v in ev.items() if k != "type"}
    return final


def llm_ready(llm: dict | None) -> bool:
    return bool(llm and llm.get("enabled") and llm.get("api_key") and llm.get("model"))


def not_configured_reply() -> dict:
    return {"reply": "No language model is configured. Set up a provider and API "
                     "key in Settings → AI assistant, then try again.",
            "tool_calls": [], "journal": [], "iterations": 0, "stopped": False,
            "needs_config": True}


# -- OpenAI / Anthropic tool-calling translators ------------------------------

def _openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": t} for t in tools]


def _to_openai_messages(convo: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in convo:
        role = m["role"]
        if role == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"],
                        "content": m["content"]})
        elif role == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or None,
                        "tool_calls": [
                            {"id": tc["id"], "type": "function",
                             "function": {"name": tc["name"],
                                          "arguments": json.dumps(tc.get("args") or {})}}
                            for tc in m["tool_calls"]]})
        else:
            out.append({"role": role, "content": m.get("content", "")})
    return out


def _parse_openai(data: dict) -> CallerResult:
    msg = data["choices"][0]["message"]
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": tc.get("id", fn.get("name")), "name": fn.get("name"),
                      "args": args})
    return {"text": msg.get("content") or "", "tool_calls": calls}


def _anthropic_tools(tools: list[dict]) -> list[dict]:
    return [{"name": t["name"], "description": t["description"],
             "input_schema": t["parameters"]} for t in tools]


def _to_anthropic_messages(convo: list[dict]) -> tuple[str, list[dict]]:
    system = "\n".join(m["content"] for m in convo
                       if m["role"] == "system" and m.get("content"))
    msgs: list[dict] = []
    for m in convo:
        role = m["role"]
        if role == "system":
            continue
        if role == "tool":
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": m["tool_call_id"],
                 "content": m["content"]}]})
        elif role == "assistant" and m.get("tool_calls"):
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                blocks.append({"type": "tool_use", "id": tc["id"],
                               "name": tc["name"], "input": tc.get("args") or {}})
            msgs.append({"role": "assistant", "content": blocks})
        else:
            msgs.append({"role": role, "content": m.get("content", "")})
    return system, msgs


def _parse_anthropic(data: dict) -> CallerResult:
    text_parts, calls = [], []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append({"id": block.get("id"), "name": block.get("name"),
                          "args": block.get("input") or {}})
    return {"text": "".join(text_parts), "tool_calls": calls}


def provider_tool_call(convo: list[dict], tools: list[dict], llm: dict) -> CallerResult:
    """One model turn against the configured provider, returning the neutral
    ``{text, tool_calls}`` shape. Branches by provider (OpenAI / Anthropic)."""
    provider = (llm.get("provider") or "openai").lower()
    if provider == "anthropic":
        system, msgs = _to_anthropic_messages(convo)
        body: dict[str, Any] = {"model": llm["model"], "max_tokens": 2000,
                                "messages": msgs, "tools": _anthropic_tools(tools)}
        if system:
            body["system"] = system
        data = _http_post_json(
            llm["base_url"],
            {"x-api-key": llm["api_key"], "anthropic-version": "2023-06-01"}, body)
        return _parse_anthropic(data)

    body = {"model": llm["model"], "messages": _to_openai_messages(convo),
            "tools": _openai_tools(tools), "temperature": 0}
    data = _http_post_json(
        llm["base_url"], {"Authorization": f"Bearer {llm['api_key']}"}, body)
    return _parse_openai(data)

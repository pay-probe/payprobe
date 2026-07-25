"""The provider-neutral, tool-calling agent loop + OpenAI/Anthropic adapters.

Self-contained: the conversation is kept in an internal role format and one
model turn is delegated to a ``caller`` (the real provider in prod, a scripted
stub in tests). Tool calls are dispatched through :mod:`assistant_service.tools`.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from . import rest
from .tools import ToolContext, dispatch, schemas_for

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
    "before creating or changing anything, and prefer editing what exists.\n"
    "- Use ONLY the provided tools to make changes; never claim a change you did "
    "not perform with a tool.\n"
    "- Writes apply immediately but are reversible — explain what you changed.\n"
    "- If a tool errors or is blocked by a guardrail, explain it instead of "
    "retrying blindly.\n"
    "- PORTAL GEOGRAPHY (when telling the user where to click): scenarios are "
    "built and RUN from the Scenarios editor's Run button, and re-run from the "
    "Runs page; /load is load testing; /model-studio is ML training only "
    "(datasets + custom categorizer models — scenarios never appear there); "
    "networks are authored and started on the Networks canvas. Never invent "
    "pages or buttons — if unsure where something lives, say what the API/tool "
    "does instead.\n"
    "- Be concise."
)

CallerResult = dict  # {"text": str, "tool_calls": [{"id","name","args"}]}
Caller = Callable[[list[dict], list[dict], dict], CallerResult]

# Plan mode: the model SEES write-tool schemas (so it can propose exact calls)
# but execution is gated to read tier; it ends its reply with a machine-readable
# plan the UI turns into Apply/Skip cards (executed via POST /agent/apply —
# the same journalled, guardrailed dispatch path as full mode).
PLAN_SYSTEM = (
    "\nPLAN MODE — you CANNOT make changes in this conversation; write tools "
    "are visible but disabled. When the user asks for changes: inspect current "
    "state with read tools, then END your reply with a fenced json block of "
    'the exact calls you would make: ```json\n{"plan": [{"tool": '
    '"<write tool name>", "args": {…}, "summary": "one short line"}]}\n``` '
    "The user applies each item individually, so keep items independent and "
    "ordered. Use real tool names and complete args. If nothing needs "
    "changing, answer normally without a plan block."
)

_PLAN_RE = None  # compiled lazily


def extract_plan(reply: str) -> tuple[str, list[dict]]:
    """Split a plan-mode reply into (clean text, plan actions).

    Looks for the LAST fenced ```json block containing a top-level "plan"
    array; malformed blocks are left in the text untouched (never lose what
    the model said)."""
    global _PLAN_RE
    if _PLAN_RE is None:
        import re
        _PLAN_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
    matches = list(_PLAN_RE.finditer(reply or ""))
    for m in reversed(matches):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        plan = data.get("plan")
        if not isinstance(plan, list):
            continue
        actions = [
            {"tool": str(a.get("tool", "")), "args": a.get("args") or {},
             "summary": str(a.get("summary", ""))}
            for a in plan if isinstance(a, dict) and a.get("tool")
        ]
        clean = (reply[:m.start()] + reply[m.end():]).strip()
        return clean, actions
    return reply, []


def llm_ready(llm: dict | None) -> bool:
    return bool(llm and llm.get("enabled") and llm.get("api_key") and llm.get("model"))


def not_configured_reply() -> dict:
    return {"reply": "No language model is configured. Add a provider and API "
                     "key in Settings → AI assistant, or set ASSIST_LLM_API_KEY "
                     "(and provider/model) on the assistant service.",
            "tool_calls": [], "journal": [], "iterations": 0, "stopped": False,
            "needs_config": True}


def iter_agent(messages: list[dict], ctx: ToolContext, llm: dict, *,
               tiers: tuple[str, ...] = ("read", "write", "execute"),
               max_iterations: int = MAX_ITERATIONS,
               caller: Caller | None = None,
               stream: bool = False,
               exec_tiers: tuple[str, ...] | None = None,
               extra_system: str | None = None):
    """Run the loop, yielding progress events: a ``tool`` event (phase
    start/end, tool name only — args may carry secrets) per step, then exactly
    one ``final`` event with reply/tool_calls/journal/iterations/stopped.

    With ``stream=True`` (and no injected caller) the provider is called in
    SSE mode and ``{"type": "delta", "text": …}`` events are yielded as the
    model writes — for every model turn, not only the last; the UI shows them
    live and replaces them with the turn's outcome (tool activity or final).

    ``tiers`` controls which tool SCHEMAS the model sees; ``exec_tiers``
    (default: same) controls which tools may actually EXECUTE — plan mode
    shows write schemas but executes reads only. ``extra_system`` appends
    mode-specific instructions to the system prompt."""
    call = caller or provider_tool_call
    tools = schemas_for(tiers)
    run_tiers = exec_tiers if exec_tiers is not None else tiers
    system = SYSTEM_PROMPT + (extra_system or "")
    convo: list[dict] = [{"role": "system", "content": system}, *messages]
    executed: list[dict] = []

    for i in range(1, max_iterations + 1):
        if stream and caller is None:
            resp = {}
            for ev in provider_stream_events(convo, tools, llm):
                if ev["type"] == "delta":
                    yield ev
                else:  # "result" — exactly one, always last
                    resp = ev["result"]
        else:
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
            out = dispatch(ctx, tc["name"], tc.get("args") or {},
                           tiers=run_tiers)
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


def run_agent(messages: list[dict], ctx: ToolContext, llm: dict, *,
              tiers: tuple[str, ...] = ("read", "write", "execute"),
              max_iterations: int = MAX_ITERATIONS,
              caller: Caller | None = None,
              exec_tiers: tuple[str, ...] | None = None,
              extra_system: str | None = None) -> dict:
    """Non-streaming run; same contract as before, built on :func:`iter_agent`."""
    final: dict = {}
    for ev in iter_agent(messages, ctx, llm, tiers=tiers,
                         max_iterations=max_iterations, caller=caller,
                         exec_tiers=exec_tiers, extra_system=extra_system):
        if ev.get("type") == "final":
            final = {k: v for k, v in ev.items() if k != "type"}
    return final


# -- provider translators -----------------------------------------------------

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
            a = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            a = {}
        calls.append({"id": tc.get("id", fn.get("name")), "name": fn.get("name"),
                      "args": a})
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
    provider = (llm.get("provider") or "openai").lower()
    if provider == "anthropic":
        system, msgs = _to_anthropic_messages(convo)
        body: dict[str, Any] = {"model": llm["model"], "max_tokens": 2000,
                                "messages": msgs, "tools": _anthropic_tools(tools)}
        if system:
            body["system"] = system
        data = rest.post_json(
            llm["base_url"],
            {"x-api-key": llm["api_key"], "anthropic-version": "2023-06-01"}, body)
        return _parse_anthropic(data)

    body = {"model": llm["model"], "messages": _to_openai_messages(convo),
            "tools": _openai_tools(tools), "temperature": 0}
    data = rest.post_json(
        llm["base_url"], {"Authorization": f"Bearer {llm['api_key']}"}, body)
    return _parse_openai(data)


# -- provider streaming (SSE) -------------------------------------------------

def _sse_data_events(lines) -> "Any":
    """Parse raw SSE lines into decoded ``data:`` JSON payloads (dicts)."""
    for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def _stream_openai(convo: list[dict], tools: list[dict], llm: dict):
    body = {"model": llm["model"], "messages": _to_openai_messages(convo),
            "tools": _openai_tools(tools), "temperature": 0, "stream": True}
    lines = rest.post_stream(
        llm["base_url"], {"Authorization": f"Bearer {llm['api_key']}"}, body)
    text_parts: list[str] = []
    calls_by_index: dict[int, dict] = {}
    for chunk in _sse_data_events(lines):
        choices = chunk.get("choices") or []
        if not choices:
            continue  # keep-alive / usage frames
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            text_parts.append(delta["content"])
            yield {"type": "delta", "text": delta["content"]}
        for tc in delta.get("tool_calls") or []:
            slot = calls_by_index.setdefault(
                tc.get("index", 0), {"id": None, "name": None, "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]
    calls = []
    for _, slot in sorted(calls_by_index.items()):
        try:
            args = json.loads(slot["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": slot["id"] or slot["name"], "name": slot["name"],
                      "args": args})
    yield {"type": "result",
           "result": {"text": "".join(text_parts), "tool_calls": calls}}


def _stream_anthropic(convo: list[dict], tools: list[dict], llm: dict):
    system, msgs = _to_anthropic_messages(convo)
    body: dict[str, Any] = {"model": llm["model"], "max_tokens": 2000,
                            "messages": msgs, "tools": _anthropic_tools(tools),
                            "stream": True}
    if system:
        body["system"] = system
    lines = rest.post_stream(
        llm["base_url"],
        {"x-api-key": llm["api_key"], "anthropic-version": "2023-06-01"}, body)
    text_parts: list[str] = []
    blocks: dict[int, dict] = {}  # index → {"type", "id", "name", "partial_json"}
    for ev in _sse_data_events(lines):
        etype = ev.get("type")
        if etype == "content_block_start":
            block = ev.get("content_block") or {}
            blocks[ev.get("index", 0)] = {
                "type": block.get("type"), "id": block.get("id"),
                "name": block.get("name"), "partial_json": ""}
        elif etype == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                text_parts.append(delta["text"])
                yield {"type": "delta", "text": delta["text"]}
            elif delta.get("type") == "input_json_delta":
                slot = blocks.setdefault(
                    ev.get("index", 0),
                    {"type": "tool_use", "id": None, "name": None,
                     "partial_json": ""})
                slot["partial_json"] += delta.get("partial_json", "")
    calls = []
    for _, slot in sorted(blocks.items()):
        if slot.get("type") != "tool_use":
            continue
        try:
            args = json.loads(slot["partial_json"] or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": slot.get("id") or slot.get("name"),
                      "name": slot.get("name"), "args": args})
    yield {"type": "result",
           "result": {"text": "".join(text_parts), "tool_calls": calls}}


def provider_stream_events(convo: list[dict], tools: list[dict], llm: dict):
    """One streamed model turn: yields ``delta`` events then one ``result``.

    If the stream cannot be opened (provider without SSE support, proxy that
    strips it, …) this falls back to the blocking call — same result event,
    just without live deltas."""
    provider = (llm.get("provider") or "openai").lower()
    streamer = _stream_anthropic if provider == "anthropic" else _stream_openai
    try:
        yield from streamer(convo, tools, llm)
    except RuntimeError:
        yield {"type": "result", "result": provider_tool_call(convo, tools, llm)}

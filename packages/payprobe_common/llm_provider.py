"""Provider-neutral LLM tool calling — ONCE, for every caller.

The OpenAI and Anthropic request/response shapes for one tool-calling turn.
Historically private to the assistant's ``loop.py``; agent-hub's heartbeat
runner (ADR-0010 phase 2) needs the same call, and a second copy is the drift
CLAUDE.md invariant #3 records. Transport is injected (``post_json(url,
headers, body) -> dict``) so callers keep their own timeouts and error mapping.

``llm`` is the dict :func:`assistant_service.config.resolve_llm` produces:
``provider``, ``model``, ``base_url``, ``api_key``.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypedDict


class CallerResult(TypedDict):
    text: str
    tool_calls: list[dict]     # [{"id", "name", "args"}]


PostJson = Callable[[str, dict, dict], dict]


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


def provider_tool_call(convo: list[dict], tools: list[dict], llm: dict,
                       post_json: PostJson, *, temperature: float = 0.0,
                       max_tokens: int = 2000) -> CallerResult:
    provider = (llm.get("provider") or "openai").lower()
    if provider == "anthropic":
        system, msgs = _to_anthropic_messages(convo)
        body: dict[str, Any] = {"model": llm["model"], "max_tokens": max_tokens,
                                "messages": msgs, "tools": _anthropic_tools(tools)}
        if system:
            body["system"] = system
        data = post_json(
            llm["base_url"],
            {"x-api-key": llm["api_key"], "anthropic-version": "2023-06-01"}, body)
        return _parse_anthropic(data)

    body = {"model": llm["model"], "messages": _to_openai_messages(convo),
            "tools": _openai_tools(tools), "temperature": temperature}
    data = post_json(
        llm["base_url"], {"Authorization": f"Bearer {llm['api_key']}"}, body)
    return _parse_openai(data)




def usage_of(data: dict, provider: str) -> dict:
    """Token usage from a raw provider response, normalized to
    ``{"input": n, "output": n}`` (zeros when the provider omits it)."""
    u = data.get("usage") or {}
    if provider == "anthropic":
        return {"input": int(u.get("input_tokens") or 0),
                "output": int(u.get("output_tokens") or 0)}
    return {"input": int(u.get("prompt_tokens") or 0),
            "output": int(u.get("completion_tokens") or 0)}

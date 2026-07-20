"""AI scenario assistant (v0).

Turns a natural-language prompt into a **Starter-Flow-shaped** chain the editor
can insert directly. Two providers behind one interface:

* ``heuristic`` (default) — deterministic, grounded intent matching against the
  live catalog. No API key, fully offline + testable.
* ``llm`` — used only when an API key is configured (``ASSIST_LLM_KEY`` /
  ``OPENAI_API_KEY``). Sends the trimmed catalog + prompt and parses JSON.

Every produced step is **grounded**: its ``(target, action)`` must exist in the
catalog, so the output is always insertable. Assertions encode the intent the
user described (approved / declined / settled …).
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from models import STEP_CATALOG


def catalog_index() -> dict[str, set[str]]:
    return {t.target: {a.name for a in t.actions} for t in STEP_CATALOG}


def catalog_prompt() -> str:
    """A detailed, model-facing listing: target/action(params) -> response_fields."""
    lines = []
    for t in STEP_CATALOG:
        for a in t.actions:
            params = ",".join(p.name for p in (a.params or []))
            rf = ",".join(a.response_fields or [])
            lines.append(f"{t.target}/{a.name}({params}) -> {rf}")
    return "\n".join(lines)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def ground_steps(raw: list[dict], idx: dict[str, set[str]]) -> tuple[list[dict], list[str]]:
    """Keep only steps whose (target, action) exist — tolerantly. Matches exact,
    then normalized (case/spacing), then remaps by a unique action name. Returns
    (grounded_steps, dropped_descriptions)."""
    norm_target = {_norm(t): t for t in idx}
    norm_action = {(t, _norm(a)): a for t, acts in idx.items() for a in acts}
    action_targets: dict[str, set[str]] = {}
    for t, acts in idx.items():
        for a in acts:
            action_targets.setdefault(_norm(a), set()).add(t)

    grounded, dropped = [], []
    for s in raw:
        tgt, act = s.get("target", ""), s.get("action", "")
        if tgt in idx and act in idx[tgt]:
            grounded.append(s)
            continue
        ct = tgt if tgt in idx else norm_target.get(_norm(tgt))
        if ct:
            ca = act if act in idx[ct] else norm_action.get((ct, _norm(act)))
            if ca:
                s["target"], s["action"] = ct, ca
                grounded.append(s)
                continue
        owners = action_targets.get(_norm(act))           # remap by unique action
        if owners and len(owners) == 1:
            ct = next(iter(owners))
            s["target"], s["action"] = ct, norm_action[(ct, _norm(act))]
            grounded.append(s)
            continue
        dropped.append(f"{tgt}/{act}")
    return grounded, dropped


def _a(field: str, operator: str, expected: Any = None) -> dict:
    out = {"field": field, "operator": operator}
    if expected is not None:
        out["expected"] = expected
    return out


_TXN_WORDS = re.compile(
    r"auth|purchase|sale|payment|transaction|buy|checkout|charge|pos|terminal|"
    r"\bcard\b|tap|chip|contactless|approv|declin|settle|reversal|refund|swipe|magstripe")


def build_steps(prompt: str, idx: dict[str, set[str]]) -> list[dict]:
    """Grounded heuristic: assemble a realistic multi-step chain from the prompt.

    A payment is the default intent, so most prompts produce a full POS flow
    (card → PIN → authorize → settlement → remove card). Pure-utility prompts
    (echo / ISO 8583 / crypto only) build just those. Add-ons (reversal, refund,
    echo, ISO, PIN block) are appended when mentioned.
    """
    p = prompt.lower()
    steps: list[dict] = []

    def add(ref, target, action, config=None, inputs=None, assertions=None) -> None:
        if target in idx and action in idx.get(target, set()):
            steps.append({"ref": ref, "target": target, "action": action,
                          "config": config or {}, "inputs": inputs or {},
                          "assertions": assertions or []})

    declined = bool(re.search(r"declin|reject|insufficient|do ?not ?honou?r|over.?limit|pick.?up", p))
    minimal = bool(re.search(r"\bonly\b|\bjust\b|single step|one step|auth(orization)? only", p))

    wants_echo = bool(re.search(r"echo|network management|sign[- ]?on|0800|heartbeat|reachab", p))
    wants_iso = bool(re.search(r"iso ?8583|pack|parse|wire message|bitmap", p))
    wants_pinblk = bool(re.search(r"pin ?block", p))
    wants_crypto = bool(re.search(r"arqc|arpc|cryptogram|session key|emv crypto", p))

    # only build the utility steps when the prompt is *purely* utility (no txn cues)
    utility_only = (wants_echo or wants_iso or wants_pinblk or wants_crypto) \
        and not _TXN_WORDS.search(p)

    if not utility_only:
        # 1) authorization — the flow entry point
        auth_asserts = ([_a("response_code", "ne", "00")] if declined
                        else [_a("response_code", "eq", "00"), _a("auth_code", "present")])
        add("auth", "http", "send_auth_request",
            {"amount": 10000, "currency": "GEL"}, assertions=auth_asserts)
        # 2) downstream settlement check (approved flows only)
        if not minimal and not declined:
            add("settle", "db_probe_core", "query_transaction",
                {"rrn": "${auth.response.rrn}"},
                assertions=[_a("status", "eq", "APPROVED")])

    # add-ons (also valid as standalone utility flows)
    if re.search(r"revers", p):
        add("reversal", "http", "send_reversal", {"rrn": "${auth.response.rrn}"},
            assertions=[_a("response_code", "eq", "00")])
    if re.search(r"refund", p):
        add("refund", "http", "send_refund",
            {"rrn": "${auth.response.rrn}", "amount": 10000},
            assertions=[_a("response_code", "eq", "00")])
    if wants_echo:
        add("echo", "http", "echo_test", assertions=[_a("status", "eq", "ok")])
    if wants_iso:
        add("pack", "iso_messaging", "iso8583_pack")
        add("parse", "iso_messaging", "iso8583_parse",
            inputs={"message": "${pack.response.message}"})
    if wants_pinblk:
        add("pinblk", "emv_crypto", "pin_block")

    return steps


def build_edit_steps(prompt: str, idx: dict[str, set[str]],
                     existing: list[dict]) -> list[dict]:
    """Extend an existing scenario: emit only NEW steps, wired to existing nodes
    by their real ids (so '${<id>.response.rrn}' references survive insertion)."""
    p = prompt.lower()
    steps: list[dict] = []

    def find(target: str, action: str) -> str | None:
        return next((e["id"] for e in existing
                     if e.get("target") == target and e.get("action") == action), None)

    auth_id = find("http", "send_auth_request")
    rrn_ref = f"${{{auth_id}.response.rrn}}" if auth_id else "${auth.response.rrn}"

    def add(ref, target, action, config=None, inputs=None, assertions=None) -> None:
        if target in idx and action in idx.get(target, set()):
            steps.append({"ref": ref, "target": target, "action": action,
                          "config": config or {}, "inputs": inputs or {},
                          "assertions": assertions or []})

    if re.search(r"settle|settlement|downstream|database|\bdb\b|posted|reconcil", p):
        add("settle", "db_probe_core", "query_transaction", {"rrn": rrn_ref},
            assertions=[_a("status", "eq", "APPROVED")])
    if re.search(r"revers", p):
        add("reversal", "http", "send_reversal", {"rrn": rrn_ref},
            assertions=[_a("response_code", "eq", "00")])
    if re.search(r"refund", p):
        add("refund", "http", "send_refund", {"rrn": rrn_ref},
            assertions=[_a("response_code", "eq", "00")])
    if re.search(r"echo|network|reachab|0800|heartbeat", p):
        add("echo", "http", "echo_test", assertions=[_a("status", "eq", "ok")])
    if re.search(r"pin ?block", p):
        add("pinblk", "emv_crypto", "pin_block")
    if re.search(r"auth|purchase|sale|payment", p) and not auth_id:
        add("auth", "http", "send_auth_request",
            {"amount": 10000, "currency": "GEL"},
            assertions=[_a("response_code", "eq", "00"), _a("auth_code", "present")])

    return steps


def _label(prompt: str) -> str:
    s = prompt.strip().rstrip(".")
    return (s[:60] + "…") if len(s) > 60 else (s or "AI scenario")


#: last LLM error, surfaced to the assist response so failures aren't silent
_LAST_LLM_ERROR: str | None = None


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.rsplit("```", 1)[0]
    return t.strip()


def _http_post_json(url: str, headers: dict, body: dict) -> dict:
    """POST JSON and return the parsed response (stdlib — no extra dependency)."""
    import json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"connection error: {exc.reason}")


def _llm_call(messages: list[dict], llm: dict, *, json_mode: bool) -> str:
    """One chat call -> the assistant message text. Branches by provider."""
    provider = (llm.get("provider") or "openai").lower()

    if provider == "anthropic":
        # Anthropic Messages API: system is top-level; messages are user/assistant
        # only; auth via x-api-key; response is content[].text.
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        chat = [{"role": m["role"], "content": m["content"]}
                for m in messages if m["role"] != "system"]
        body = {"model": llm["model"], "max_tokens": 2000, "messages": chat}
        if system:
            body["system"] = system
        data = _http_post_json(
            llm["base_url"],
            {"x-api-key": llm["api_key"], "anthropic-version": "2023-06-01"},
            body)
        parts = data.get("content") or []
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text")

    # OpenAI-compatible chat completions
    body = {"model": llm["model"], "messages": messages, "temperature": 0}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    data = _http_post_json(
        llm["base_url"], {"Authorization": f"Bearer {llm['api_key']}"}, body)
    return data["choices"][0]["message"]["content"]


def _llm_flow(prompt: str, idx: dict[str, set[str]], llm: dict | None) -> dict | None:
    """Optional LLM provider — used only when ``llm`` is enabled with a key.
    Returns a flow dict, or None to fall back (recording why in _LAST_LLM_ERROR)."""
    global _LAST_LLM_ERROR
    _LAST_LLM_ERROR = None
    if not llm or not llm.get("enabled") or not llm.get("api_key"):
        return None
    try:  # pragma: no cover - requires network + key
        import json
        example = (
            '{"label":"Card purchase approved","steps":['
            '{"ref":"auth","target":"http","action":"send_auth_request",'
            '"config":{"amount":10000,"currency":"GEL"},'
            '"assertions":[{"field":"response_code","operator":"eq","expected":"00"}]},'
            '{"ref":"settle","target":"db_probe_core","action":"query_transaction",'
            '"config":{"rrn":"${auth.response.rrn}"},'
            '"assertions":[{"field":"status","operator":"eq","expected":"APPROVED"}]}]}')
        sys_prompt = (
            "You author PayProbe payment-test scenarios. Output ONLY a JSON object "
            '{"label","description","steps":[{"ref","target","action","config":{},'
            '"inputs":{},"assertions":[{"field","operator","expected"}]}]}.\n'
            "Rules: use ONLY the exact target/action ids from the catalog below "
            "(format target/action(params) -> response_fields). Put parameter "
            "values in `config`. Wire a later step to an earlier one with "
            "${ref.response.FIELD} (use the listed response_fields). Operators: "
            "eq, ne, gt, lt, present, absent.\nCATALOG:\n" + catalog_prompt()
            + "\nEXAMPLE:\n" + example)
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt}]
        try:
            content = _llm_call(messages, llm, json_mode=True)
        except Exception:
            # some providers reject response_format -> retry without it
            content = _llm_call(messages, llm, json_mode=False)
        flow = json.loads(_strip_fences(content))
        if isinstance(flow.get("steps"), list):
            return flow
        _LAST_LLM_ERROR = "model returned JSON without a 'steps' array"
        return None
    except Exception as exc:  # pragma: no cover
        _LAST_LLM_ERROR = f"{type(exc).__name__}: {exc}"
        return None


def _key_provider_mismatch(provider: str, key: str) -> str | None:
    """A clear hint when the key prefix doesn't match the selected provider."""
    if key.startswith("sk-ant-") and provider != "anthropic":
        return ("This looks like an Anthropic key (sk-ant-…) but Provider is set "
                "to OpenAI. Switch Provider to “Anthropic (Claude)”, Save, then Test.")
    if provider == "anthropic" and key.startswith("sk-") and not key.startswith("sk-ant-"):
        return ("This looks like an OpenAI key but Provider is set to Anthropic. "
                "Switch Provider to “OpenAI (GPT)”, Save, then Test.")
    return None


def _model_examples(provider: str) -> str:
    if provider == "anthropic":
        return ("Examples: claude-opus-4-8, claude-sonnet-4-6, "
                "claude-3-5-sonnet-latest.")
    return "Examples: gpt-4o, gpt-4o-mini, gpt-4-turbo."


def _model_format_hint(provider: str, model: str) -> str | None:
    """Catch obviously-invalid model ids before wasting an API round-trip."""
    model = (model or "").strip()
    if not model:
        return "No model id set. " + _model_examples(provider)
    if " " in model:
        return f"“{model}” isn't a valid model id (ids have no spaces). " + _model_examples(provider)
    if provider == "anthropic" and not model.startswith("claude"):
        return (f"“{model}” isn't a valid Anthropic model — ids start with "
                f"'claude-'. " + _model_examples(provider))
    return None


def test_llm(llm: dict | None) -> dict:
    """Make a minimal real call to verify the configured LLM (for Settings)."""
    if not llm or not llm.get("api_key"):
        return {"ok": False, "enabled": bool(llm and llm.get("enabled")),
                "error": "No API key configured (save your key first)."}
    provider = llm.get("provider", "openai")
    mismatch = _key_provider_mismatch(provider, llm["api_key"])
    if mismatch:
        return {"ok": False, "enabled": bool(llm.get("enabled")),
                "model": llm.get("model"), "error": mismatch}
    bad_model = _model_format_hint(provider, llm.get("model", ""))
    if bad_model:
        return {"ok": False, "enabled": bool(llm.get("enabled")),
                "model": llm.get("model"), "error": bad_model}
    try:
        reply = _llm_call(
            [{"role": "user", "content": "Reply with the single word: OK"}],
            llm, json_mode=False)
        return {"ok": True, "enabled": bool(llm.get("enabled")),
                "model": llm.get("model"), "reply": (reply or "").strip()[:80]}
    except Exception as exc:
        msg = str(exc)
        if "not_found" in msg.lower() or "model" in msg.lower() and "404" in msg:
            msg += " — check the model id is valid for this provider/account."
        return {"ok": False, "enabled": bool(llm.get("enabled")),
                "model": llm.get("model"), "error": f"{type(exc).__name__}: {msg}"}


_EDIT_RE = re.compile(r"\b(add|also|then|insert|append|include|another|extra)\b")


def assist(prompt: str, scenario_steps: list[dict] | None = None,
           llm: dict | None = None) -> dict:
    idx = catalog_index()
    existing = scenario_steps or []
    is_edit = bool(existing) and bool(_EDIT_RE.search(prompt.lower()))
    provider = "heuristic"
    flow = _llm_flow(prompt, idx, llm)
    if flow:
        provider = "llm"
    else:
        if is_edit:
            steps = build_edit_steps(prompt, idx, existing) or build_steps(prompt, idx)
        else:
            steps = build_steps(prompt, idx)
        flow = {"label": _label(prompt), "description": prompt.strip(), "steps": steps}

    # ground (tolerantly) the produced steps against the catalog
    grounded, dropped = ground_steps(flow.get("steps", []), idx)

    # if the LLM produced nothing usable, fall back to the heuristic so the user
    # always gets a starting flow rather than an empty result
    if not grounded and provider == "llm":
        steps = (build_edit_steps(prompt, idx, existing) if is_edit
                 else build_steps(prompt, idx))
        grounded, _ = ground_steps(steps, idx)
        provider = "heuristic-fallback"

    flow["steps"] = grounded
    flow["id"] = f"ai-{int(time.time())}"
    flow.setdefault("builtin", False)

    notes = []
    if provider in ("heuristic", "heuristic-fallback") and llm and llm.get("enabled") \
            and llm.get("api_key") and _LAST_LLM_ERROR:
        notes.append(f"LLM unavailable, used the built-in heuristic — {_LAST_LLM_ERROR}. "
                     "Check Settings → Test connection.")
    if dropped:
        notes.append("Skipped steps the catalog doesn't have: "
                     + ", ".join(dropped[:6]) + (" …" if len(dropped) > 6 else ""))
    if not grounded:
        notes.append("Could not map the request to known steps — try naming an "
                     "action (auth, settlement, echo, ISO 8583 pack…).")
    return {"flow": flow, "provider": provider, "mode": "extend" if is_edit else "create",
            "notes": notes}

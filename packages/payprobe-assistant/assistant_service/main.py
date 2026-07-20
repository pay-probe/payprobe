"""PayProbe Assistant — FastAPI app.

Endpoints:
* ``GET  /health``        — liveness.
* ``POST /agent/chat``    — multi-turn, tool-calling config assistant.
* ``POST /agent/revert``  — undo every change made in a session.

The service holds the LLM provider credentials (single egress boundary) and
calls the PayProbe services over REST with a service token. Run:

    SCENARIO_API_URL=http://localhost:8000 RUN_API_URL=http://localhost:8001 \
        ASSIST_LLM_API_KEY=sk-... uvicorn assistant_service.main:app --port 8400
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from fastapi.responses import StreamingResponse

from .auth import require_auth
from .config import resolve_llm
from .history import build_chat_history
from .loop import (
    PLAN_SYSTEM,
    extract_plan,
    iter_agent,
    llm_ready,
    not_configured_reply,
    run_agent,
)
from .session import build_session_store
from .tools import (
    ChangeJournal,
    ToolContext,
    dispatch,
    referenced_connection_names,
    restore_journal,
    restore_one,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.sessions = build_session_store()
    app.state.chats = build_chat_history()
    yield


def _caller_sub(request: Request) -> str:
    """Stable per-user key for chat history (JWT sub; dev mode = anonymous)."""
    claims = getattr(request.state, "auth", None) or {}
    return str(claims.get("sub") or "anonymous")


app = FastAPI(title="PayProbe Assistant", version="0.1.0", lifespan=lifespan,
              # every route (except auth.PUBLIC_PATHS) requires a caller
              # credential — the assistant makes autonomous writes with
              # service credentials, so its own door must be locked too.
              dependencies=[Depends(require_auth)])

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "CORS_ORIGINS", "http://localhost:4200,http://127.0.0.1:4200").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    mode: Literal["full", "advisor", "plan"] = "full"
    session_id: str | None = None


def _mode_kwargs(mode: str) -> dict:
    """Loop parameters per assistant mode.

    full    — read+write+execute schemas, read+write+execute execution
              (execute = playground ad-hoc fires, ADR-0007; never journalled).
    advisor — read schemas, read execution.
    plan    — read+write+execute schemas (the model must know the args to
              propose them) but READ-ONLY execution + plan instructions; the
              user applies proposed actions via /agent/apply."""
    if mode == "advisor":
        return {"tiers": ("read",)}
    if mode == "plan":
        return {"tiers": ("read", "write", "execute"), "exec_tiers": ("read",),
                "extra_system": PLAN_SYSTEM}
    return {"tiers": ("read", "write", "execute")}


class RevertRequest(BaseModel):
    session_id: str


@app.get("/health")
async def health() -> dict:
    # resolve_llm may fetch the Settings-stored config → threadpool.
    llm = await run_in_threadpool(resolve_llm)
    return {"status": "ok", "service": "payprobe-assistant",
            "llm_configured": llm_ready(llm),
            "llm_source": llm.get("source")}


@app.post("/insights/explain/{run_id}")
async def explain_insights(run_id: str) -> dict:
    """Phase-2 (ADR-0005): LLM-written prose over the insight service's
    deterministic evidence packs. The model sees only the packs — this
    service is the LLM egress boundary, the insight service never calls out.
    Falls back to the standard needs-config reply when no LLM is set."""
    from .explain import explain_run

    llm = await run_in_threadpool(resolve_llm)
    if not llm_ready(llm):
        return not_configured_reply()
    try:
        return await run_in_threadpool(explain_run, run_id, llm)
    except RuntimeError as exc:  # insight service or provider unreachable
        return {"run_id": run_id, "explanations": [],
                "error": str(exc), "advisory_only": True}


class SuggestLabelsRequest(BaseModel):
    limit: int = 8


@app.post("/insights/suggest-labels")
async def suggest_labels_endpoint(req: SuggestLabelsRequest) -> dict:
    """LLM label suggestions for the active-learning queue (ADR-0005 egress
    boundary: only this service talks to providers; the model sees only the
    normalized shapes + the existing taxonomy). Suggestions only — the human
    approves each one in the portal before anything is written."""
    from .suggest import suggest_labels

    llm = await run_in_threadpool(resolve_llm)
    if not llm_ready(llm):
        return not_configured_reply()
    limit = max(1, min(25, req.limit))
    try:
        return await run_in_threadpool(suggest_labels, limit, llm)
    except RuntimeError as exc:  # insight service or provider unreachable
        return {"suggestions": [], "error": str(exc), "advisory_only": True}


async def _append_session_journal(request: Request, session_id: str,
                                  records: list[dict],
                                  ui_entries: list[dict] | None) -> None:
    """Persist a turn's journal under the session, re-sequencing so `seq` is
    unique across the whole session (each turn's ChangeJournal restarts at 1 —
    per-change revert needs a stable session-wide id). The UI `journal`
    entries are renumbered in lockstep so revert-one targets match."""
    if not records:
        return
    existing = await request.app.state.sessions.get(session_id)
    base = max((r.get("seq", 0) for r in existing), default=0)
    for i, rec in enumerate(records, 1):
        rec["seq"] = base + i
    for i, entry in enumerate(ui_entries or [], 1):
        entry["seq"] = base + i
    await request.app.state.sessions.append(session_id, records)


@app.post("/agent/chat")
async def chat(req: ChatRequest, request: Request) -> dict:
    """Multi-turn config assistant. Writes (full mode) are journalled + revertible."""
    llm = await run_in_threadpool(resolve_llm)
    if not llm_ready(llm):
        return not_configured_reply()
    referenced = await run_in_threadpool(referenced_connection_names)
    ctx = ToolContext(journal=ChangeJournal(), referenced_connections=referenced)
    msgs = [{"role": m.role, "content": m.content} for m in req.messages]
    kw = _mode_kwargs(req.mode)
    result = await run_in_threadpool(
        lambda: run_agent(msgs, ctx, llm, **kw))
    if req.mode == "plan":
        result["reply"], result["plan"] = extract_plan(result.get("reply", ""))
    session_id = req.session_id or uuid.uuid4().hex
    await _append_session_journal(request, session_id, ctx.journal.dump(),
                                  result.get("journal"))
    result["session_id"] = session_id
    return result


@app.post("/agent/chat/stream")
async def chat_stream(req: ChatRequest, request: Request) -> StreamingResponse:
    """Streaming variant: SSE ``tool`` events (live status) then one ``final``."""
    import asyncio
    import json
    import threading
    import uuid

    llm = await run_in_threadpool(resolve_llm)
    loop = asyncio.get_running_loop()

    async def gen():
        if not llm_ready(llm):
            yield f'data: {json.dumps({"type": "final", **not_configured_reply()})}\n\n'
            return
        referenced = await run_in_threadpool(referenced_connection_names)
        ctx = ToolContext(journal=ChangeJournal(), referenced_connections=referenced)
        msgs = [{"role": m.role, "content": m.content} for m in req.messages]
        kw = _mode_kwargs(req.mode)
        q: asyncio.Queue = asyncio.Queue()

        def worker():
            try:
                for ev in iter_agent(msgs, ctx, llm, stream=True, **kw):
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(
                    q.put_nowait,
                    {"type": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=worker, daemon=True).start()
        session_id = req.session_id or uuid.uuid4().hex
        while True:
            ev = await q.get()
            if ev is None:
                break
            if ev.get("type") == "final":
                if req.mode == "plan":
                    ev["reply"], ev["plan"] = extract_plan(ev.get("reply", ""))
                await _append_session_journal(
                    request, session_id, ctx.journal.dump(), ev.get("journal"))
                ev["session_id"] = session_id
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/agent/revert")
async def revert(req: RevertRequest, request: Request) -> dict:
    """Undo every change the assistant made in a session, newest first."""
    records = await request.app.state.sessions.get(req.session_id)
    if not records:
        return {"reverted": 0, "session_id": req.session_id}
    n = await run_in_threadpool(restore_journal, records)
    await request.app.state.sessions.clear(req.session_id)
    return {"reverted": n, "session_id": req.session_id}


class ApplyAction(BaseModel):
    tool: str
    args: dict = {}


class ApplyRequest(BaseModel):
    session_id: str | None = None
    actions: list[ApplyAction]


@app.post("/agent/apply")
async def apply_plan(req: ApplyRequest, request: Request) -> dict:
    """Execute plan-mode actions the user approved.

    Same journalled, guardrailed dispatch path as full-mode writes (invariant:
    the registry is the bouncer — a stale or hallucinated plan item fails the
    same way a bad live call would). Only write-tier tools are accepted."""
    referenced = await run_in_threadpool(referenced_connection_names)
    ctx = ToolContext(journal=ChangeJournal(), referenced_connections=referenced)
    results = [
        await run_in_threadpool(dispatch, ctx, a.tool, a.args, ("write",))
        for a in req.actions
    ]
    session_id = req.session_id or uuid.uuid4().hex
    journal = ctx.journal.entries()
    await _append_session_journal(request, session_id, ctx.journal.dump(),
                                  journal)
    return {"session_id": session_id, "results": results, "journal": journal}


class ToolRequest(BaseModel):
    tool: str
    args: dict = {}


@app.post("/agent/tool")
async def run_read_tool(req: ToolRequest) -> dict:
    """Direct READ-tool dispatch — no model round-trip.

    Powers the portal's slash commands (/health, /runs, …) and the @-mention
    catalog. Write tools are refused here; they go through chat (journalled)
    or /agent/apply."""
    ctx = ToolContext(journal=ChangeJournal())
    return await run_in_threadpool(dispatch, ctx, req.tool, req.args, ("read",))


# -- server-side chat history (per caller) -------------------------------------

@app.get("/agent/chats")
async def list_chats(request: Request) -> dict:
    """The caller's saved conversations, newest first (summaries + turns —
    documents are portal-shaped and opaque to the service)."""
    chats = await request.app.state.chats.list(_caller_sub(request))
    return {"chats": chats}


class ChatDoc(BaseModel):
    id: str
    model_config = {"extra": "allow"}  # title/mode/turns/… are portal-owned


@app.put("/agent/chats/{chat_id}")
async def put_chat(chat_id: str, doc: ChatDoc, request: Request) -> dict:
    body = doc.model_dump()
    body["id"] = chat_id
    await request.app.state.chats.put(_caller_sub(request), body)
    return {"ok": True, "id": chat_id}


@app.delete("/agent/chats/{chat_id}")
async def delete_chat(chat_id: str, request: Request) -> dict:
    await request.app.state.chats.delete(_caller_sub(request), chat_id)
    return {"ok": True, "id": chat_id}


@app.delete("/agent/chats")
async def clear_chats(request: Request) -> dict:
    await request.app.state.chats.clear(_caller_sub(request))
    return {"ok": True}


@app.get("/agent/session/{session_id}/changes")
async def session_changes(session_id: str, request: Request) -> dict:
    """Full journal records for a session (incl. before/after snapshots) —
    feeds the portal's per-change diff view."""
    records = await request.app.state.sessions.get(session_id)
    return {"session_id": session_id, "changes": records}


class RevertOneRequest(BaseModel):
    session_id: str
    seq: int


@app.post("/agent/revert-one")
async def revert_one_change(req: RevertOneRequest, request: Request) -> dict:
    """Undo a single journalled change.

    Only the NEWEST change for its (resource, key) may be undone in isolation:
    restoring an older record would clobber the later writes on the same
    resource (restore is a pure function of `before` — invariant #2). Callers
    get a clear error naming the blocking change instead."""
    records = await request.app.state.sessions.get(req.session_id)
    target = next((r for r in records if r.get("seq") == req.seq), None)
    if target is None:
        return {"reverted": 0, "session_id": req.session_id,
                "error": "no such change in this session"}
    blocker = next(
        (r for r in records
         if r.get("seq", 0) > req.seq
         and r.get("resource") == target.get("resource")
         and r.get("key") == target.get("key")), None)
    if blocker is not None:
        return {"reverted": 0, "session_id": req.session_id,
                "error": f"a newer change (#{blocker['seq']}: "
                         f"{blocker.get('summary', '')}) touches the same "
                         f"{target.get('resource')} — undo that one first"}
    await run_in_threadpool(restore_one, target)
    remaining = [r for r in records if r.get("seq") != req.seq]
    await request.app.state.sessions.replace(req.session_id, remaining)
    return {"reverted": 1, "session_id": req.session_id, "seq": req.seq}


class TitleRequest(BaseModel):
    messages: list[ChatMessage]


_TITLE_SYSTEM = (
    "You title chat conversations for a history list. Reply with ONLY the "
    "title: 3-6 words, plain text, no quotes, no trailing punctuation."
)


@app.post("/agent/title")
async def title(req: TitleRequest) -> dict:
    """Short display title for a saved conversation (one-shot, tool-free).

    Advisory nicety: the portal falls back to its derived title (truncated
    first user message) whenever this returns empty, errors, or the LLM is
    not configured — so this endpoint never blocks the chat flow."""
    from .explain import completion

    llm = await run_in_threadpool(resolve_llm)
    if not llm_ready(llm):
        return {"title": "", **not_configured_reply()}
    convo = "\n".join(
        f"{m.role}: {m.content[:400]}" for m in req.messages[:4] if m.content)
    if not convo:
        return {"title": ""}
    try:
        text = await run_in_threadpool(completion, _TITLE_SYSTEM, convo, llm)
    except RuntimeError as exc:  # provider unreachable — caller keeps fallback
        return {"title": "", "error": str(exc)}
    cleaned = " ".join(text.strip().strip("\"'`").split())
    if len(cleaned) > 60:
        cleaned = cleaned[:59].rstrip() + "…"
    return {"title": cleaned}

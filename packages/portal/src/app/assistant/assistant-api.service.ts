import { HttpClient } from "@angular/common/http";
import { Injectable, inject } from "@angular/core";
import { Observable } from "rxjs";

import { AuthService } from "../auth/auth.service";
import { RuntimeConfigService } from "../shared/runtime-config.service";
import {
  AgentChatResult,
  AssistantMode,
  ChatMessage,
  JournalEntry,
  PlanAction,
  RevertResult,
  SessionChange,
  StoredConversation,
  StreamEvent,
  ToolCallResult,
} from "./assistant.models";

/** Typed client for the general config assistant (standalone payprobe-assistant). */
@Injectable({ providedIn: "root" })
export class AssistantApiService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);
  private readonly auth = inject(AuthService);
  /** Resolved per call; the standalone payprobe-assistant since the
   *  2026-07-07 cutover (scenario-service's /agent/* shim is deprecated but
   *  still answers if the endpoint override points there). */
  private get base(): string {
    return this.cfg.assistantApiBase;
  }

  chat(
    messages: ChatMessage[],
    mode: AssistantMode = "full",
    sessionId?: string,
  ): Observable<AgentChatResult> {
    return this.http.post<AgentChatResult>(`${this.base}/agent/chat`, {
      messages,
      mode,
      session_id: sessionId,
    });
  }

  revert(sessionId: string): Observable<RevertResult> {
    return this.http.post<RevertResult>(`${this.base}/agent/revert`, {
      session_id: sessionId,
    });
  }

  /** Execute approved plan-mode actions (journalled write path). */
  apply(
    sessionId: string | undefined,
    actions: PlanAction[],
  ): Observable<{
    session_id: string;
    results: ToolCallResult[];
    journal: JournalEntry[];
  }> {
    return this.http.post<{
      session_id: string;
      results: ToolCallResult[];
      journal: JournalEntry[];
    }>(`${this.base}/agent/apply`, {
      session_id: sessionId,
      actions: actions.map((a) => ({ tool: a.tool, args: a.args })),
    });
  }

  /** Direct read-tool dispatch — powers slash commands and @-mentions. */
  tool(tool: string, args: object = {}): Observable<ToolCallResult> {
    return this.http.post<ToolCallResult>(`${this.base}/agent/tool`, {
      tool,
      args,
    });
  }

  // -- server-side chat history --

  listChats(): Observable<{ chats: StoredConversation[] }> {
    return this.http.get<{ chats: StoredConversation[] }>(
      `${this.base}/agent/chats`,
    );
  }

  putChat(doc: StoredConversation): Observable<{ ok: boolean }> {
    return this.http.put<{ ok: boolean }>(
      `${this.base}/agent/chats/${encodeURIComponent(doc.id)}`,
      doc,
    );
  }

  deleteChat(id: string): Observable<{ ok: boolean }> {
    return this.http.delete<{ ok: boolean }>(
      `${this.base}/agent/chats/${encodeURIComponent(id)}`,
    );
  }

  clearChats(): Observable<{ ok: boolean }> {
    return this.http.delete<{ ok: boolean }>(`${this.base}/agent/chats`);
  }

  /** Full journal records (incl. before/after snapshots) for the diff view. */
  sessionChanges(
    sessionId: string,
  ): Observable<{ session_id: string; changes: SessionChange[] }> {
    return this.http.get<{ session_id: string; changes: SessionChange[] }>(
      `${this.base}/agent/session/${encodeURIComponent(sessionId)}/changes`,
    );
  }

  /** Undo one journalled change. The backend refuses (with `error`) when a
   *  newer change touches the same resource — undo newest-first. */
  revertOne(
    sessionId: string,
    seq: number,
  ): Observable<{ reverted: number; error?: string }> {
    return this.http.post<{ reverted: number; error?: string }>(
      `${this.base}/agent/revert-one`,
      { session_id: sessionId, seq },
    );
  }

  /** Short LLM-written title for the history list. Advisory: the caller keeps
   *  its derived title when this returns empty / needs_config / errors. */
  title(
    messages: ChatMessage[],
  ): Observable<{ title: string; needs_config?: boolean; error?: string }> {
    return this.http.post<{
      title: string;
      needs_config?: boolean;
      error?: string;
    }>(`${this.base}/agent/title`, { messages });
  }

  /** Phase-2 insight explanations (ADR-0005): the assistant's LLM rewrites
   *  the insight service's deterministic evidence packs as plain prose. The
   *  model sees only the packs; needs ASSIST_LLM_* configured. */
  explainRun(runId: string): Observable<{
    run_id: string;
    explanations: { scenario_id: string; step_id: string; text: string }[];
    needs_config?: boolean;
    error?: string;
  }> {
    return this.http.post<{
      run_id: string;
      explanations: { scenario_id: string; step_id: string; text: string }[];
      needs_config?: boolean;
      error?: string;
    }>(`${this.base}/insights/explain/${encodeURIComponent(runId)}`, {});
  }

  /** LLM label suggestions for the Model Studio label queue (ADR-0005
   *  egress boundary). Suggestions only — the human approves each teach. */
  suggestLabels(limit = 8): Observable<{
    suggestions: {
      signature: string;
      sample_text: string;
      label: string;
      reason: string;
      is_new: boolean;
    }[];
    needs_config?: boolean;
    error?: string;
  }> {
    return this.http.post<{
      suggestions: {
        signature: string;
        sample_text: string;
        label: string;
        reason: string;
        is_new: boolean;
      }[];
      needs_config?: boolean;
      error?: string;
    }>(`${this.base}/insights/suggest-labels`, { limit });
  }

  /**
   * Stream a chat turn via SSE, invoking `onEvent` for each event (tool steps
   * then a final). Uses fetch (HttpClient can't stream); attaches the bearer
   * token manually since the interceptor only sees HttpClient requests. Rejects
   * if the stream can't be opened, so the caller can fall back to {@link chat}.
   */
  async streamChat(
    messages: ChatMessage[],
    mode: AssistantMode,
    sessionId: string | undefined,
    onEvent: (ev: StreamEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const token = this.auth.token();
    const res = await fetch(`${this.base}/agent/chat/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ messages, mode, session_id: sessionId }),
      signal,
    });
    if (!res.ok || !res.body) throw new Error(`stream failed: ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of block.split("\n")) {
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          try {
            onEvent(JSON.parse(payload) as StreamEvent);
          } catch {
            /* ignore malformed SSE line */
          }
        }
      }
    }
  }
}

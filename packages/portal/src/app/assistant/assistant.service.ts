import { Injectable, computed, inject, signal } from "@angular/core";
import { Router } from "@angular/router";

import { UiService } from "../shared/ui.service";
import { AssistantApiService } from "./assistant-api.service";
import {
  AssistantMode,
  ChatMessage,
  DisplayTurn,
  MentionItem,
  PlanAction,
  SessionChange,
  StoredConversation,
} from "./assistant.models";

const HISTORY_KEY = "payprobe.assistant.history";
const MAX_CONVERSATIONS = 40;

/**
 * App-wide state for the configuration assistant slide-over.
 *
 * Holds the active conversation, open/mode/busy state, and orchestrates calls to
 * {@link AssistantApiService}. Past conversations are auto-saved to localStorage
 * and browsable from the history list; selecting one reloads its transcript.
 * Page context (the current route) is prepended to the first user turn so the
 * assistant knows where the user is, without polluting the visible transcript.
 *
 * Writes accumulate under a server-side session; {@link revert} undoes every
 * change made in that session with one click.
 */
@Injectable({ providedIn: "root" })
export class AssistantService {
  private readonly api = inject(AssistantApiService);
  private readonly ui = inject(UiService);
  private readonly router = inject(Router);

  readonly open = signal(false);
  readonly mode = signal<AssistantMode>("full");
  readonly busy = signal(false);
  readonly reverting = signal(false);
  readonly turns = signal<DisplayTurn[]>([]);
  /** Live progress line shown while a turn streams (e.g. "Reading connections…"). */
  readonly status = signal<string | null>(null);
  /** Token-level text of the in-flight model turn (grows with each delta;
   *  cleared when tools start or the final reply lands). */
  readonly streamText = signal("");
  /** Unsent input, kept across panel close/reopen (and set by edit-and-resend). */
  readonly draft = signal("");
  /** before/after snapshots by seq, fetched lazily for the diff view. */
  readonly changeDetails = signal<Record<number, SessionChange>>({});
  /** @-mentionable resources (fetched lazily, cached for the session). */
  readonly mentionItems = signal<MentionItem[]>([]);
  private mentionsLoaded = false;
  /** Whether server-side history has been merged in this app session. */
  private historySynced = false;

  /** Abort handle for the in-flight turn (Stop button). */
  private abortCtl: AbortController | null = null;
  private stopRequested = false;

  /** LLM-written titles by conversation id (fallback: derived title). */
  private readonly llmTitles = new Map<string, string>();
  private readonly titleAttempted = new Set<string>();

  /** Server session the change journal accumulates under. */
  private readonly sessionId = signal<string | null>(null);
  /** How many reversible changes have been applied this session. */
  readonly changeCount = signal(0);

  /** Saved conversations (newest first) + whether the history list is showing. */
  readonly conversations = signal<StoredConversation[]>(this.load());
  readonly historyOpen = signal(false);
  private readonly currentId = signal<string>(this.newId());

  readonly hasConversation = computed(() => this.turns().length > 0);
  readonly canRevert = computed(
    () => this.changeCount() > 0 && !!this.sessionId(),
  );

  constructor() {
    // Reloaded conversations keep their LLM titles (and are not re-titled).
    for (const c of this.conversations()) {
      if (c.titleSource === "llm") {
        this.llmTitles.set(c.id, c.title);
        this.titleAttempted.add(c.id);
      }
    }
    // Cmd/Ctrl+K toggles the panel anywhere; Esc closes it. The service is
    // root-scoped and lives for the app's lifetime, so no teardown needed.
    document.addEventListener("keydown", (e: KeyboardEvent) => {
      if (
        (e.metaKey || e.ctrlKey) &&
        !e.shiftKey &&
        !e.altKey &&
        e.key.toLowerCase() === "k"
      ) {
        e.preventDefault();
        this.toggle();
      } else if (e.key === "Escape" && this.open()) {
        this.close();
      }
    });
  }

  toggle(): void {
    this.open.update((v) => !v);
    if (this.open()) this.syncHistory();
  }

  close(): void {
    this.open.set(false);
  }

  setMode(mode: AssistantMode): void {
    this.mode.set(mode);
  }

  // -- history ----------------------------------------------------------------

  toggleHistory(): void {
    this.historyOpen.update((v) => !v);
  }

  /** Start a fresh conversation (the current one is already auto-saved). */
  reset(): void {
    this.turns.set([]);
    this.sessionId.set(null);
    this.changeCount.set(0);
    this.changeDetails.set({});
    this.currentId.set(this.newId());
    this.historyOpen.set(false);
  }

  /** Reopen a saved conversation. Revert is disabled for reloaded history (the
   *  live change journal isn't restored), so changeCount stays 0. */
  loadConversation(id: string): void {
    const convo = this.conversations().find((c) => c.id === id);
    if (!convo) return;
    this.currentId.set(convo.id);
    this.mode.set(convo.mode);
    this.sessionId.set(convo.sessionId);
    this.turns.set(convo.turns.map((t) => ({ ...t })));
    this.changeCount.set(0);
    this.changeDetails.set({});
    this.historyOpen.set(false);
  }

  deleteConversation(id: string): void {
    const next = this.conversations().filter((c) => c.id !== id);
    this.conversations.set(next);
    this.persist(next);
    this.api.deleteChat(id).subscribe({ next: () => {}, error: () => {} });
    if (id === this.currentId()) this.reset();
  }

  clearHistory(): void {
    this.conversations.set([]);
    this.persist([]);
    this.api.clearChats().subscribe({ next: () => {}, error: () => {} });
    this.reset();
  }

  // -- chat -------------------------------------------------------------------

  send(text: string): void {
    const content = text.trim();
    if (!content || this.busy()) return;
    this.draft.set("");
    this.turns.update((t) => [...t, { role: "user", content }]);
    this.dispatch();
  }

  /** Re-send the conversation after dropping the failed/unwanted reply. */
  retry(): void {
    if (this.busy()) return;
    const turns = this.turns();
    const idx = turns.map((t) => t.role).lastIndexOf("user");
    if (idx < 0) return;
    this.turns.set(turns.slice(0, idx + 1));
    this.dispatch();
  }

  /** Pull the last user message back into the input for editing; the message
   *  (and anything after it) leaves the transcript. */
  editLast(): string | null {
    if (this.busy()) return null;
    const turns = this.turns();
    const idx = turns.map((t) => t.role).lastIndexOf("user");
    if (idx < 0) return null;
    const text = turns[idx].content;
    this.turns.set(turns.slice(0, idx));
    this.draft.set(text);
    this.saveCurrent();
    return text;
  }

  /** Follow-up after the agent stopped at the tool-step limit. */
  continueRun(): void {
    this.send("Continue where you left off.");
  }

  /** Open the panel and ask about a specific thing on screen. `question` is
   *  shown in the transcript; `context` rides along invisibly (entity ids,
   *  error details, …) so the assistant starts grounded. */
  askAbout(question: string, context: string): void {
    if (this.busy()) return;
    this.open.set(true);
    this.historyOpen.set(false);
    this.turns.update((t) => [
      ...t,
      { role: "user", content: question, context },
    ]);
    this.dispatch();
  }

  /** Abort the in-flight turn. Changes already applied by completed tool calls
   *  remain (and stay revertible once a later turn reports its journal). */
  stop(): void {
    if (!this.busy()) return;
    this.stopRequested = true;
    this.abortCtl?.abort();
  }

  /** Send the current transcript to the agent and stream the reply. */
  private dispatch(): void {
    this.busy.set(true);
    this.status.set("Thinking…");
    this.saveCurrent(); // keep the user's message even if the reply fails

    // role+content history for the backend; turns can carry hidden context
    // (entity the user was looking at) and the first turn carries the route.
    const turns = this.turns();
    const history: ChatMessage[] = turns.map((t, i) => {
      let content = t.content;
      if (t.context) content = `[Context: ${t.context}]\n${content}`;
      if (i === 0 && turns.length === 1 && !t.context) {
        content = `[Context: the user is on the ${this.router.url} page.]\n${content}`;
      }
      return { role: t.role, content };
    });

    this.stopRequested = false;
    this.abortCtl = new AbortController();
    let finalized = false;
    this.api
      .streamChat(
        history,
        this.mode(),
        this.sessionId() ?? undefined,
        (ev) => {
          if (ev.type === "delta" && ev.text) {
            this.streamText.update((s) => s + ev.text);
          } else if (ev.type === "tool" && ev.phase === "start") {
            // that model turn's text ended in tool calls — show tool status
            this.streamText.set("");
            this.status.set(this.humanizeTool(ev.tool ?? ""));
          } else if (ev.type === "final") {
            finalized = true;
            this.applyFinal(ev);
          } else if (ev.type === "error") {
            finalized = true;
            this.pushError("The assistant hit an error. Please try again.");
          }
        },
        this.abortCtl.signal,
      )
      .then(() => {
        if (!finalized)
          this.pushError(
            "The assistant returned no response. Please try again.",
          );
        this.finish();
      })
      .catch(() => {
        if (this.stopRequested) {
          // user pressed Stop — not a failure, no fallback
          this.pushError("Stopped.");
          this.finish();
          return;
        }
        // streaming not available → fall back to the plain request
        this.fallback(history);
      });
  }

  /** Non-streaming fallback when SSE can't be opened. */
  private fallback(history: ChatMessage[]): void {
    this.api
      .chat(history, this.mode(), this.sessionId() ?? undefined)
      .subscribe({
        next: (res) => {
          this.applyFinal(res);
          this.finish();
        },
        error: () => {
          this.pushError(
            "Couldn't reach the assistant. Check the service is running.",
          );
          this.finish();
          this.ui.toast("error", "Assistant request failed.");
        },
      });
  }

  /** Apply a final result (streamed or not): the reply + its tool calls/journal. */
  private applyFinal(res: {
    needs_config?: boolean;
    session_id?: string;
    reply?: string;
    tool_calls?: DisplayTurn["toolCalls"];
    journal?: DisplayTurn["journal"];
    plan?: PlanAction[];
    stopped?: boolean;
  }): void {
    if (res.needs_config) {
      this.pushError(
        "No language model is configured. Open Settings → AI assistant, add a " +
          "provider and API key (applies within seconds), or set ASSIST_LLM_* " +
          "in the assistant service's environment.",
      );
      return;
    }
    if (res.session_id) this.sessionId.set(res.session_id);
    this.changeCount.update((n) => n + (res.journal?.length ?? 0));
    this.turns.update((t) => [
      ...t,
      {
        role: "assistant",
        content: res.reply ?? "",
        toolCalls: res.tool_calls,
        journal: res.journal,
        plan: res.plan?.length
          ? res.plan.map((p) => ({ ...p, state: "pending" as const }))
          : undefined,
        stopped: res.stopped,
      },
    ]);
  }

  // -- plan mode: apply / skip proposed actions -------------------------------

  /** Apply one proposed action via the journalled write path. */
  applyAction(turnIndex: number, actionIndex: number): void {
    const action = this.turns()[turnIndex]?.plan?.[actionIndex];
    if (!action || action.state !== "pending" || this.busy()) return;
    this.api.apply(this.sessionId() ?? undefined, [action]).subscribe({
      next: (res) => {
        this.sessionId.set(res.session_id);
        const out = res.results[0];
        this.setPlanState(
          turnIndex,
          actionIndex,
          out?.ok ? "applied" : "failed",
          out?.ok ? undefined : (out?.error ?? "failed"),
        );
        if (res.journal.length) {
          // merge into the turn's journal so diff view + undo just work
          this.turns.update((turns) =>
            turns.map((t, i) =>
              i === turnIndex
                ? { ...t, journal: [...(t.journal ?? []), ...res.journal] }
                : t,
            ),
          );
          this.changeCount.update((n) => n + res.journal.length);
          this.changeDetails.set({}); // refetch lazily with the new records
        }
        this.saveCurrent();
      },
      error: () => {
        this.setPlanState(turnIndex, actionIndex, "failed", "request failed");
      },
    });
  }

  /** Apply every still-pending action of a plan, in order. */
  applyAll(turnIndex: number): void {
    const plan = this.turns()[turnIndex]?.plan ?? [];
    plan.forEach((p, i) => {
      if (p.state === "pending") this.applyAction(turnIndex, i);
    });
  }

  skipAction(turnIndex: number, actionIndex: number): void {
    this.setPlanState(turnIndex, actionIndex, "skipped");
    this.saveCurrent();
  }

  private setPlanState(
    turnIndex: number,
    actionIndex: number,
    state: "pending" | "applied" | "skipped" | "failed",
    error?: string,
  ): void {
    this.turns.update((turns) =>
      turns.map((t, i) =>
        i === turnIndex && t.plan
          ? {
              ...t,
              plan: t.plan.map((p, j) =>
                j === actionIndex ? { ...p, state, error } : p,
              ),
            }
          : t,
      ),
    );
  }

  // -- slash commands: direct read-tool dispatch (no model round-trip) --------

  /** Run a read tool directly and render its result as a turn. */
  runReadTool(command: string, tool: string): void {
    if (this.busy()) return;
    this.turns.update((t) => [...t, { role: "user", content: command }]);
    this.busy.set(true);
    this.status.set(this.humanizeTool(tool));
    this.api.tool(tool).subscribe({
      next: (out) => {
        this.turns.update((t) => [
          ...t,
          {
            role: "assistant",
            content: out.ok ? "" : (out.error ?? "Tool failed."),
            error: !out.ok,
            toolCalls: [out],
          },
        ]);
        this.finish();
      },
      error: () => {
        this.pushError("Couldn't reach the assistant service.");
        this.finish();
      },
    });
  }

  // -- @-mention catalog -------------------------------------------------------

  /** Fetch mentionable resource names (once per app session, best-effort). */
  loadMentions(): void {
    if (this.mentionsLoaded) return;
    this.mentionsLoaded = true;
    const sources: [
      string,
      string,
      (item: Record<string, unknown>) => string,
    ][] = [
      ["list_connections", "connection", (r) => String(r["name"] ?? "")],
      ["list_scenarios", "scenario", (r) => String(r["name"] ?? r["id"] ?? "")],
      ["list_networks", "network", (r) => String(r["name"] ?? r["id"] ?? "")],
      ["list_tables", "table", (r) => String(r["name"] ?? "")],
      [
        "list_starter_flows",
        "flow",
        (r) => String(r["label"] ?? r["id"] ?? ""),
      ],
      ["list_environments", "environment", (r) => String(r["name"] ?? "")],
    ];
    for (const [tool, kind, pick] of sources) {
      this.api.tool(tool).subscribe({
        next: (out) => {
          if (!out.ok || !Array.isArray(out.result)) return;
          const items = (out.result as Record<string, unknown>[])
            .map((r) => ({ name: pick(r), kind }))
            .filter((m) => m.name);
          this.mentionItems.update((all) => [...all, ...items]);
        },
        error: () => {
          /* catalog stays partial */
        },
      });
    }
  }

  // -- server-side history sync -------------------------------------------------

  /** Merge server-stored chats into the local list (newest updatedAt wins).
   *  Local stays the offline fallback; the server copy survives browsers. */
  private syncHistory(): void {
    if (this.historySynced) return;
    this.historySynced = true;
    this.api.listChats().subscribe({
      next: (res) => {
        const byId = new Map<string, StoredConversation>();
        for (const c of this.conversations()) byId.set(c.id, c);
        for (const s of res.chats ?? []) {
          const local = byId.get(s.id);
          if (!local || (s.updatedAt ?? 0) > (local.updatedAt ?? 0)) {
            byId.set(s.id, s);
          }
        }
        const merged = [...byId.values()].sort(
          (a, b) => b.updatedAt - a.updatedAt,
        );
        this.conversations.set(merged.slice(0, MAX_CONVERSATIONS));
        this.persist(this.conversations());
        for (const c of merged) {
          if (c.titleSource === "llm" && !this.llmTitles.has(c.id)) {
            this.llmTitles.set(c.id, c.title);
            this.titleAttempted.add(c.id);
          }
        }
      },
      error: () => {
        this.historySynced = false; // retry on next open
      },
    });
  }

  private pushError(message: string): void {
    this.turns.update((t) => [
      ...t,
      { role: "assistant", content: message, error: true },
    ]);
  }

  private finish(): void {
    this.busy.set(false);
    this.status.set(null);
    this.streamText.set("");
    this.abortCtl = null;
    this.saveCurrent();
    this.maybeTitle();
  }

  /** Ask the backend for a short LLM title once the first exchange lands.
   *  One attempt per conversation; silent fallback to the derived title. */
  private maybeTitle(): void {
    const id = this.currentId();
    if (this.titleAttempted.has(id)) return;
    const turns = this.turns();
    if (!turns.some((t) => t.role === "assistant" && !t.error)) return;
    this.titleAttempted.add(id);
    const messages: ChatMessage[] = turns
      .slice(0, 4)
      .map((t) => ({ role: t.role, content: t.content }));
    this.api.title(messages).subscribe({
      next: (res) => {
        const title = (res.title ?? "").trim();
        if (!title) return; // needs_config / provider error → keep derived
        this.llmTitles.set(id, title);
        const next = this.conversations().map((c) =>
          c.id === id ? { ...c, title, titleSource: "llm" as const } : c,
        );
        this.conversations.set(next);
        this.persist(next);
      },
      error: () => this.titleAttempted.delete(id), // retry on a later turn
    });
  }

  /** Friendly status label from a tool name (e.g. list_connections → "Reading
   *  connections…", upsert_connection → "Saving connection…"). */
  private humanizeTool(tool: string): string {
    const words = tool.split("_");
    const verb = words[0];
    const rest = words.slice(1).join(" ") || tool;
    const map: Record<string, string> = {
      list: "Reading",
      get: "Reading",
      validate: "Validating",
      upsert: "Saving",
      save: "Saving",
      create: "Saving",
      update: "Saving",
      set: "Saving",
      delete: "Deleting",
    };
    const label = map[verb];
    return label ? `${label} ${rest}…` : `Running ${tool}…`;
  }

  /** Undo every change the assistant made this session. */
  revert(): void {
    const sid = this.sessionId();
    if (!sid || this.reverting()) return;
    this.reverting.set(true);
    this.api.revert(sid).subscribe({
      next: (res) => {
        this.changeCount.set(0);
        this.turns.update((t) => [
          ...t,
          {
            role: "assistant",
            content:
              res.reverted > 0
                ? `Reverted ${res.reverted} change${res.reverted === 1 ? "" : "s"}.`
                : "There was nothing to revert.",
          },
        ]);
        this.reverting.set(false);
        this.saveCurrent();
        if (res.reverted > 0)
          this.ui.toast("success", `Reverted ${res.reverted} changes.`);
      },
      error: () => {
        this.reverting.set(false);
        this.ui.toast("error", "Revert failed.");
      },
    });
  }

  /** Undo one journalled change. The backend enforces newest-first per
   *  (resource, key); a refusal surfaces as a toast with its reason. */
  revertOne(seq: number): void {
    const sid = this.sessionId();
    if (!sid || this.reverting()) return;
    this.reverting.set(true);
    this.api.revertOne(sid, seq).subscribe({
      next: (res) => {
        this.reverting.set(false);
        if (res.reverted !== 1) {
          this.ui.toast("error", res.error ?? "Couldn't undo that change.");
          return;
        }
        // mark the entry reverted wherever it appears in the transcript
        this.turns.update((turns) =>
          turns.map((t) =>
            t.journal?.some((j) => j.seq === seq)
              ? {
                  ...t,
                  journal: t.journal!.map((j) =>
                    j.seq === seq ? { ...j, reverted: true } : j,
                  ),
                }
              : t,
          ),
        );
        this.changeCount.update((n) => Math.max(0, n - 1));
        this.saveCurrent();
        this.ui.toast("success", "Change undone.");
      },
      error: () => {
        this.reverting.set(false);
        this.ui.toast("error", "Undo failed.");
      },
    });
  }

  /** Fetch before/after snapshots for the diff view (cached per session). */
  loadChangeDetails(): void {
    const sid = this.sessionId();
    if (!sid) return;
    this.api.sessionChanges(sid).subscribe({
      next: (res) => {
        const map: Record<number, SessionChange> = {};
        for (const c of res.changes) map[c.seq] = c;
        this.changeDetails.set(map);
      },
      error: () => {
        /* diff view stays summary-only */
      },
    });
  }

  // -- persistence ------------------------------------------------------------

  /** Upsert the active conversation into the saved list (newest first). */
  private saveCurrent(): void {
    const turns = this.turns();
    if (turns.length === 0) return;
    const llmTitle = this.llmTitles.get(this.currentId());
    const convo: StoredConversation = {
      id: this.currentId(),
      title: llmTitle ?? this.deriveTitle(turns),
      titleSource: llmTitle ? "llm" : undefined,
      mode: this.mode(),
      sessionId: this.sessionId(),
      turns: turns.map((t) => this.stripForStorage(t)),
      updatedAt: Date.now(),
    };
    const next = [
      convo,
      ...this.conversations().filter((c) => c.id !== convo.id),
    ].slice(0, MAX_CONVERSATIONS);
    this.conversations.set(next);
    this.persist(next);
    // best-effort server copy (cross-device; localStorage stays the fallback)
    this.api.putChat(convo).subscribe({ next: () => {}, error: () => {} });
  }

  /** Drop bulky/sensitive tool-result payloads before writing to disk; keep the
   *  transcript, which tools ran, and the change summaries. */
  private stripForStorage(t: DisplayTurn): DisplayTurn {
    if (!t.toolCalls?.length) return t;
    return {
      ...t,
      toolCalls: t.toolCalls.map((tc) => ({
        tool: tc.tool,
        ok: tc.ok,
        tier: tc.tier,
        error: tc.error,
        guardrail: tc.guardrail,
      })),
    };
  }

  private deriveTitle(turns: DisplayTurn[]): string {
    const firstUser = turns.find((t) => t.role === "user");
    const text = (firstUser?.content ?? "New chat").trim().replace(/\s+/g, " ");
    return text.length > 48 ? text.slice(0, 47) + "…" : text;
  }

  private newId(): string {
    return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
  }

  private load(): StoredConversation[] {
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      const parsed = raw ? (JSON.parse(raw) as StoredConversation[]) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }

  private persist(list: StoredConversation[]): void {
    try {
      if (list.length === 0) localStorage.removeItem(HISTORY_KEY);
      else localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
    } catch {
      /* storage unavailable / quota — history stays in memory this session */
    }
  }
}

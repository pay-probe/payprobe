/** Types for the general configuration assistant (POST /agent/chat). */

/** A turn sent to / received from the backend (role + content only). */
export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

/** full = read+write · advisor = read-only · plan = read-only, but proposes
 *  a change set the user applies per-action via /agent/apply. */
export type AssistantMode = "full" | "advisor" | "plan";

/** One proposed action in a plan-mode reply. */
export interface PlanAction {
  tool: string;
  args: Record<string, unknown>;
  summary: string;
}

/** A plan action as rendered in the panel (client-side apply state). */
export interface PlanCard extends PlanAction {
  state: "pending" | "applied" | "skipped" | "failed";
  error?: string;
}

/** One tool the agent invoked during a turn (envelope from `dispatch`). */
export interface ToolCallResult {
  tool: string;
  ok: boolean;
  tier?: "read" | "write";
  result?: unknown;
  error?: string;
  guardrail?: boolean;
}

/** A reversible change recorded in the session change journal. */
export interface JournalEntry {
  seq: number;
  tool: string;
  resource: string;
  key: string;
  summary: string;
  /** Set client-side after a successful single-change undo. */
  reverted?: boolean;
}

/** A full journal record (incl. snapshots) from GET /agent/session/{id}/changes. */
export interface SessionChange extends JournalEntry {
  /** State before the write; null when the resource didn't exist. */
  before: unknown;
  /** State right after the write; null when the write was a delete. */
  after: unknown;
}

/** The /agent/chat response. */
export interface AgentChatResult {
  reply: string;
  tool_calls: ToolCallResult[];
  journal: JournalEntry[];
  iterations: number;
  stopped: boolean;
  /** Plan-mode only: proposed actions extracted from the reply. */
  plan?: PlanAction[];
  /** Session the change journal accumulates under (for revert). */
  session_id?: string;
  /** Set when no LLM provider is configured. */
  needs_config?: boolean;
}

/** The /agent/revert response. */
export interface RevertResult {
  reverted: number;
  session_id: string;
}

/** A Server-Sent Event from /agent/chat/stream. */
export interface StreamEvent {
  type: "delta" | "tool" | "final" | "error";
  // delta events (token-level text while the model writes)
  text?: string;
  // tool events
  phase?: "start" | "end";
  tool?: string;
  ok?: boolean;
  guardrail?: boolean;
  // final events (same shape as AgentChatResult)
  reply?: string;
  tool_calls?: ToolCallResult[];
  journal?: JournalEntry[];
  iterations?: number;
  stopped?: boolean;
  plan?: PlanAction[];
  session_id?: string;
  needs_config?: boolean;
  // error events
  error?: string;
}

/** A turn as rendered in the panel (display content + per-turn metadata). */
export interface DisplayTurn {
  role: "user" | "assistant";
  content: string;
  /** Hidden context sent to the backend with this turn (e.g. "the user is
   *  looking at run report X") but not shown in the transcript. */
  context?: string;
  toolCalls?: ToolCallResult[];
  journal?: JournalEntry[];
  /** Plan-mode: proposed actions with their Apply/Skip state. */
  plan?: PlanCard[];
  stopped?: boolean;
  error?: boolean;
}

/** One @-mentionable resource for the input autocomplete. */
export interface MentionItem {
  name: string;
  kind: string; // connection | scenario | network | table | flow | environment
}

/** A saved conversation (persisted client-side for the history list). */
export interface StoredConversation {
  id: string;
  /** Derived from the first user message, or LLM-written (see titleSource). */
  title: string;
  /** "llm" when the title came from POST /agent/title; absent when derived. */
  titleSource?: "llm";
  mode: AssistantMode;
  sessionId: string | null;
  turns: DisplayTurn[];
  updatedAt: number;
}

import {
  Component,
  ElementRef,
  OnDestroy,
  ViewChild,
  effect,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { Router } from "@angular/router";

import { IconComponent } from "../shared/ui/icon.component";
import { MentionItem } from "./assistant.models";
import { AssistantService } from "./assistant.service";
import { MarkdownPipe } from "./markdown.pipe";
import { ResultGridComponent } from "./result-grid.component";

/** A slash command: direct read-tool dispatch, or a canned prompt. */
interface SlashCommand {
  cmd: string;
  desc: string;
  tool?: string;
  prompt?: string;
}

const SLASH_COMMANDS: SlashCommand[] = [
  { cmd: "/health", tool: "platform_status", desc: "Platform status snapshot" },
  { cmd: "/runs", tool: "list_runs", desc: "Recent scenario runs" },
  { cmd: "/networks", tool: "list_networks", desc: "Saved networks" },
  { cmd: "/connections", tool: "list_connections", desc: "Saved connections" },
  {
    cmd: "/simulators",
    tool: "list_running_simulators",
    desc: "Running simulators",
  },
  { cmd: "/load", tool: "list_load_runs", desc: "Load runs" },
  {
    cmd: "/diagnose",
    prompt:
      "Check the platform's health and diagnose any problems you find, " +
      "with concrete next steps.",
    desc: "Ask for a health diagnosis",
  },
];

/**
 * Global slide-over chat panel for the configuration assistant.
 *
 * Rendered once in the app shell; opened from the topbar. It reads
 * {@link AssistantService} for conversation state, renders each assistant turn
 * with its tool calls (collapsible) and the change journal, and offers a
 * full/advisor mode toggle.
 */
@Component({
  selector: "pp-assistant-panel",
  standalone: true,
  imports: [IconComponent, ResultGridComponent, MarkdownPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (a.open()) {
      <div class="scrim" (click)="a.close()"></div>
      <aside class="panel" role="dialog" aria-label="Configuration assistant">
        <header class="head">
          <div class="title">
            <pp-icon name="sparkles" [size]="18" />
            <span>Assistant</span>
          </div>
          <div class="head-actions">
            <div class="seg" role="tablist" aria-label="Assistant mode">
              <button
                role="tab"
                [class.on]="a.mode() === 'full'"
                [attr.aria-selected]="a.mode() === 'full'"
                (click)="a.setMode('full')"
                title="Can read and make changes"
              >
                Full
              </button>
              <button
                role="tab"
                [class.on]="a.mode() === 'plan'"
                [attr.aria-selected]="a.mode() === 'plan'"
                (click)="a.setMode('plan')"
                title="Proposes changes as a plan — you apply each one"
              >
                Plan
              </button>
              <button
                role="tab"
                [class.on]="a.mode() === 'advisor'"
                [attr.aria-selected]="a.mode() === 'advisor'"
                (click)="a.setMode('advisor')"
                title="Read-only — answers without changing anything"
              >
                Advisor
              </button>
            </div>
            <button
              class="ibtn"
              [class.on]="a.historyOpen()"
              (click)="a.toggleHistory()"
              title="Chat history"
              aria-label="Chat history"
            >
              <pp-icon name="clock" [size]="18" />
            </button>
            @if (a.canRevert()) {
              <button
                class="ibtn undo"
                [disabled]="a.reverting()"
                (click)="a.revert()"
                [title]="'Undo ' + a.changeCount() + ' change(s) this session'"
              >
                <pp-icon name="undo" [size]="18" />
              </button>
            }
            @if (a.hasConversation()) {
              <button class="ibtn" (click)="a.reset()" title="New conversation">
                <pp-icon name="plus" [size]="18" />
              </button>
            }
            <button class="ibtn" (click)="a.close()" aria-label="Close">
              <pp-icon name="x" [size]="18" />
            </button>
          </div>
        </header>

        <div class="body" #scroll>
          @if (a.historyOpen()) {
            <div class="history">
              <div class="history-head">
                <span>Saved chats</span>
                @if (a.conversations().length) {
                  <button class="link" (click)="a.clearHistory()">
                    Clear all
                  </button>
                }
              </div>
              @if (a.conversations().length === 0) {
                <p class="history-empty">No saved conversations yet.</p>
              } @else {
                @for (c of a.conversations(); track c.id) {
                  <div class="hrow" (click)="a.loadConversation(c.id)">
                    <div class="hrow-main">
                      <span class="hrow-title">{{ c.title }}</span>
                      <span class="hrow-time">{{
                        relativeTime(c.updatedAt)
                      }}</span>
                    </div>
                    <button
                      class="ibtn"
                      (click)="
                        $event.stopPropagation(); a.deleteConversation(c.id)
                      "
                      aria-label="Delete conversation"
                    >
                      <pp-icon name="x" [size]="16" />
                    </button>
                  </div>
                }
              }
            </div>
          } @else {
            @if (!a.hasConversation()) {
              <div class="empty">
                <pp-icon name="sparkles" [size]="28" />
                <h4>Configure PayProbe by chatting</h4>
                <p>
                  Ask about your setup or have me change it — connections,
                  formats, tables, variables and starter flows.
                  @if (a.mode() === "full") {
                    Changes apply immediately and are listed so you can see
                    exactly what I did.
                  } @else if (a.mode() === "plan") {
                    Plan mode proposes changes as cards — nothing happens until
                    you press Apply on each one.
                  } @else {
                    Advisor mode is read-only: I’ll answer without changing
                    anything.
                  }
                  Type / for commands, &#64; to mention a resource.
                </p>
                <div class="suggest">
                  @for (s of suggestions(); track s) {
                    <button (click)="a.send(s)">{{ s }}</button>
                  }
                </div>
              </div>
            }

            @for (turn of a.turns(); track $index; let ti = $index) {
              <div class="turn" [class.user]="turn.role === 'user'">
                <!-- slash-command turns can be tool-result-only (no prose) -->
                @if (turn.content || turn.role === "user") {
                  <div class="bubble" [class.err]="turn.error">
                    @if (turn.role === "assistant" && !turn.error) {
                      <div
                        class="md"
                        [innerHTML]="turn.content | markdown"
                        (click)="onMdClick($event)"
                      ></div>
                    } @else {
                      {{ turn.content }}
                    }
                  </div>
                }

                @if (turn.role === "assistant" && !turn.error && turn.content) {
                  <div class="turn-acts">
                    <button
                      class="turn-act"
                      (click)="copyTurn($index, turn.content)"
                      title="Copy message"
                    >
                      <pp-icon
                        [name]="copiedIdx() === $index ? 'check' : 'copy'"
                        [size]="13"
                      />
                    </button>
                  </div>
                }
                @if (
                  turn.role === "user" &&
                  $index === lastUserIndex() &&
                  !a.busy()
                ) {
                  <div class="turn-acts">
                    <button
                      class="turn-act"
                      (click)="editLast()"
                      title="Edit & resend"
                    >
                      <pp-icon name="pencil" [size]="13" /> Edit
                    </button>
                  </div>
                }

                @if (
                  turn.error && $index === a.turns().length - 1 && !a.busy()
                ) {
                  <button class="chip" (click)="a.retry()">
                    <pp-icon name="rotate-cw" [size]="13" /> Retry
                  </button>
                }

                @if (turn.toolCalls?.length) {
                  <details class="tools">
                    <summary>
                      {{ turn.toolCalls!.length }}
                      tool {{ turn.toolCalls!.length === 1 ? "call" : "calls" }}
                    </summary>
                    @for (tc of turn.toolCalls!; track $index) {
                      <div class="tool" [class.ok]="tc.ok" [class.bad]="!tc.ok">
                        <pp-icon
                          [name]="tc.ok ? 'check' : 'alert-circle'"
                          [size]="14"
                        />
                        <code>{{ tc.tool }}</code>
                        @if (!tc.ok) {
                          <span class="msg">
                            {{ tc.guardrail ? "blocked: " : "" }}{{ tc.error }}
                          </span>
                        }
                      </div>
                      @if (tc.ok && tc.result) {
                        <pp-result-grid [result]="tc.result" />
                      }
                    }
                  </details>
                }

                @if (turn.journal?.length) {
                  <div class="changes">
                    <div class="changes-head">
                      <pp-icon name="check" [size]="14" /> Changes applied
                    </div>
                    @for (c of turn.journal!; track c.seq) {
                      <details
                        class="change"
                        [class.reverted]="c.reverted"
                        (toggle)="onChangeExpand($event)"
                      >
                        <summary>
                          <span class="change-summary">{{ c.summary }}</span>
                          @if (c.reverted) {
                            <span class="change-tag">undone</span>
                          } @else if (a.canRevert()) {
                            <button
                              class="turn-act"
                              [disabled]="a.reverting()"
                              (click)="
                                $event.preventDefault();
                                $event.stopPropagation();
                                a.revertOne(c.seq)
                              "
                              title="Undo just this change"
                            >
                              <pp-icon name="undo" [size]="13" />
                            </button>
                          }
                        </summary>
                        @if (!c.reverted) {
                          @if (a.changeDetails()[c.seq]; as d) {
                            <div class="diff">
                              <div class="diff-pane">
                                <div class="diff-head">Before</div>
                                <pre>{{ snapshot(d.before) }}</pre>
                              </div>
                              <div class="diff-pane">
                                <div class="diff-head">After</div>
                                <pre>{{ snapshot(d.after) }}</pre>
                              </div>
                            </div>
                          } @else {
                            <div class="diff-loading">Loading snapshots…</div>
                          }
                        }
                      </details>
                    }
                  </div>
                }

                @if (turn.plan?.length) {
                  <div class="plan">
                    <div class="plan-head">
                      <span>Proposed changes</span>
                      @if (pendingCount(turn) > 1) {
                        <button class="chip" (click)="a.applyAll(ti)">
                          Apply all
                        </button>
                      }
                    </div>
                    @for (p of turn.plan!; track $index; let ai = $index) {
                      <div class="plan-card" [class]="'plan--' + p.state">
                        <div class="plan-row">
                          <span class="plan-summary">{{
                            p.summary || p.tool
                          }}</span>
                          <code class="plan-tool">{{ p.tool }}</code>
                          @switch (p.state) {
                            @case ("pending") {
                              <button
                                class="chip"
                                (click)="a.applyAction(ti, ai)"
                              >
                                Apply
                              </button>
                              <button
                                class="chip"
                                (click)="a.skipAction(ti, ai)"
                              >
                                Skip
                              </button>
                            }
                            @case ("applied") {
                              <span class="plan-state ok">
                                <pp-icon name="check" [size]="13" /> applied
                              </span>
                            }
                            @case ("skipped") {
                              <span class="plan-state">skipped</span>
                            }
                            @case ("failed") {
                              <span class="plan-state bad">
                                {{ p.error || "failed" }}
                              </span>
                            }
                          }
                        </div>
                        <details class="plan-args">
                          <summary>args</summary>
                          <pre>{{ snapshot(p.args) }}</pre>
                        </details>
                      </div>
                    }
                  </div>
                }

                @if (turn.stopped) {
                  <div class="note">
                    Stopped at the tool-step limit.
                    @if ($index === a.turns().length - 1 && !a.busy()) {
                      <button class="chip" (click)="a.continueRun()">
                        Continue
                      </button>
                    }
                  </div>
                }
              </div>
            }

            @if (a.busy()) {
              <div class="turn">
                @if (a.streamText()) {
                  <div class="bubble">
                    <div
                      class="md"
                      [innerHTML]="a.streamText() | markdown"
                    ></div>
                  </div>
                } @else {
                  <div class="bubble thinking">
                    @if (a.status()) {
                      <span class="status-text">{{ a.status() }}</span>
                    }
                    <span class="dot"></span><span class="dot"></span
                    ><span class="dot"></span>
                  </div>
                }
              </div>
            }
          }
        </div>

        <footer class="foot">
          @if (popup() !== "none") {
            <div class="popup" role="listbox">
              @if (popup() === "slash") {
                @for (c of filteredSlash(); track c.cmd; let i = $index) {
                  <button
                    class="popup-item"
                    [class.on]="i === popupIndex()"
                    (mousedown)="$event.preventDefault(); chooseSlash(c, input)"
                  >
                    <code>{{ c.cmd }}</code>
                    <span>{{ c.desc }}</span>
                  </button>
                }
              } @else {
                @for (
                  m of filteredMentions();
                  track m.kind + m.name;
                  let i = $index
                ) {
                  <button
                    class="popup-item"
                    [class.on]="i === popupIndex()"
                    (mousedown)="
                      $event.preventDefault(); chooseMention(m, input)
                    "
                  >
                    <code>{{ m.name }}</code>
                    <span>{{ m.kind }}</span>
                  </button>
                }
              }
            </div>
          }
          <textarea
            #input
            rows="1"
            [placeholder]="placeholder()"
            (input)="onInput(input)"
            (keydown)="onKeydown($event, input)"
          ></textarea>
          @if (a.busy()) {
            <button
              class="send"
              (click)="a.stop()"
              aria-label="Stop"
              title="Stop this turn"
            >
              <pp-icon name="stop" [size]="18" />
            </button>
          } @else {
            <button class="send" (click)="submit(input)" aria-label="Send">
              <pp-icon name="send" [size]="18" />
            </button>
          }
        </footer>
      </aside>
    }
  `,
  styleUrl: "./assistant-panel.component.scss",
})
export class AssistantPanelComponent implements OnDestroy {
  readonly a = inject(AssistantService);
  private readonly router = inject(Router);
  @ViewChild("scroll") private scroll?: ElementRef<HTMLElement>;
  @ViewChild("input") private inputRef?: ElementRef<HTMLTextAreaElement>;

  /** Copy-feedback: index of the turn whose copy button shows a check. */
  readonly copiedIdx = signal(-1);

  // Empty-state chips follow the page the user is on (default mix last).
  private readonly routeChips: [RegExp, string[]][] = [
    [
      /^\/load/,
      [
        "How is the current load run doing?",
        "Compare the last load run to the previous one",
        "Which load workers are online?",
      ],
    ],
    [
      /^\/diagnostics/,
      [
        "Is the platform healthy?",
        "Why did the last run fail?",
        "What listeners are up right now?",
      ],
    ],
    [
      /^\/(networks|network-|topology)/,
      [
        "What networks do I have?",
        "What's running right now?",
        "Add a third issuer to the showcase network",
      ],
    ],
    [
      /^\/(scenarios|editor|flows)/,
      [
        "List my scenarios",
        "Create a scenario that sends an auth and asserts approval",
        "Which starter flows do I have?",
      ],
    ],
    [
      /^\/connections/,
      [
        "Add a TCP connection 'switch' at 10.0.0.10:7001",
        "Which connections are disabled?",
        "What does my 'switch' connection look like?",
      ],
    ],
    [
      /^\/(runs|reports|trends)/,
      [
        "Why did the last run fail?",
        "Which scenarios are flaky?",
        "Summarize the latest run",
      ],
    ],
    [
      /^\/(environments|secrets|test-data|variables)/,
      [
        "List my environments",
        "What global variables are set?",
        "Which test keys do I have?",
      ],
    ],
  ];
  private readonly defaultChips = [
    "What networks do I have?",
    "What's running right now?",
    "Add a TCP connection 'switch' at 10.0.0.10:7001",
  ];

  /** Chips for the empty state, chosen by the route the panel was opened on. */
  suggestions(): string[] {
    const url = this.router.url;
    for (const [re, chips] of this.routeChips) if (re.test(url)) return chips;
    return this.defaultChips;
  }

  // A rotating input placeholder that advertises what the assistant can do —
  // config AND live runtime state — so capability is discoverable without a
  // manual. (See ATLAS §6: a missing/undiscovered tool makes users misuse the
  // nearest one; teach it instead.)
  private readonly hints = [
    "Ask or tell me to configure something…",
    "e.g. what networks are running?",
    "e.g. add a third issuer to the showcase network",
    "e.g. list my connections / message formats",
    "e.g. is the platform healthy?",
    "e.g. why did the last run fail?",
    "Tip: Ctrl/⌘+K opens this panel anywhere",
  ];
  readonly placeholder = signal(this.hints[0]);
  private hintTimer?: ReturnType<typeof setInterval>;

  constructor() {
    // rotate the placeholder while the panel is idle (empty transcript)
    let i = 0;
    this.hintTimer = setInterval(() => {
      if (this.a.turns().length === 0 && !this.a.busy()) {
        i = (i + 1) % this.hints.length;
        this.placeholder.set(this.hints[i]);
      }
    }, 3500);
    // keep the transcript pinned to the latest message
    effect(() => {
      this.a.turns();
      this.a.busy();
      setTimeout(() => this.scrollToBottom(), 0);
    });
    // restore the unsent draft when the panel (re)opens
    effect(() => {
      if (this.a.open()) setTimeout(() => this.restoreDraft(), 0);
    });
  }

  ngOnDestroy(): void {
    if (this.hintTimer) clearInterval(this.hintTimer);
  }

  // -- input popups: slash commands + @-mentions ------------------------------

  readonly popup = signal<"none" | "slash" | "mention">("none");
  readonly popupIndex = signal(0);
  private readonly slashQuery = signal("");
  private readonly mentionQuery = signal("");

  filteredSlash(): SlashCommand[] {
    const q = this.slashQuery();
    return SLASH_COMMANDS.filter((c) => c.cmd.startsWith(q)).slice(0, 8);
  }

  filteredMentions(): MentionItem[] {
    const q = this.mentionQuery().toLowerCase();
    return this.a
      .mentionItems()
      .filter((m) => m.name.toLowerCase().includes(q))
      .slice(0, 8);
  }

  chooseSlash(c: SlashCommand, input: HTMLTextAreaElement): void {
    this.popup.set("none");
    input.value = "";
    this.a.draft.set("");
    this.autosize(input);
    if (c.tool) this.a.runReadTool(c.cmd, c.tool);
    else if (c.prompt) this.a.send(c.prompt);
  }

  chooseMention(m: MentionItem, input: HTMLTextAreaElement): void {
    const caret = input.selectionStart ?? input.value.length;
    const before = input.value.slice(0, caret).replace(/@[\w.-]*$/, m.name);
    input.value = before + input.value.slice(caret);
    this.a.draft.set(input.value);
    this.popup.set("none");
    this.autosize(input);
    input.focus();
    input.selectionStart = input.selectionEnd = before.length;
  }

  onKeydown(event: KeyboardEvent, input: HTMLTextAreaElement): void {
    if (this.popup() !== "none") {
      const items =
        this.popup() === "slash"
          ? this.filteredSlash().length
          : this.filteredMentions().length;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const d = event.key === "ArrowDown" ? 1 : -1;
        this.popupIndex.update((i) => (i + d + items) % Math.max(items, 1));
        return;
      }
      if ((event.key === "Enter" || event.key === "Tab") && items > 0) {
        event.preventDefault();
        const i = Math.min(this.popupIndex(), items - 1);
        if (this.popup() === "slash")
          this.chooseSlash(this.filteredSlash()[i], input);
        else this.chooseMention(this.filteredMentions()[i], input);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation(); // keep the panel open
        this.popup.set("none");
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      this.submit(input);
    }
  }

  submit(input: HTMLTextAreaElement): void {
    const text = input.value;
    if (!text.trim() || this.a.busy()) return;
    this.popup.set("none");
    // a bare known slash command submitted directly still dispatches
    const slash = SLASH_COMMANDS.find((c) => c.cmd === text.trim());
    if (slash) {
      this.chooseSlash(slash, input);
      return;
    }
    this.a.send(text);
    input.value = "";
    this.autosize(input);
  }

  /** Keep the draft in sync, grow the textarea, and drive the popups. */
  onInput(input: HTMLTextAreaElement): void {
    this.a.draft.set(input.value);
    this.autosize(input);

    const text = input.value;
    if (/^\/[\w-]*$/.test(text)) {
      this.slashQuery.set(text);
      this.popup.set("slash");
      this.popupIndex.set(0);
      return;
    }
    const caret = input.selectionStart ?? text.length;
    const mention = /@([\w.-]*)$/.exec(text.slice(0, caret));
    if (mention) {
      this.a.loadMentions();
      this.mentionQuery.set(mention[1]);
      this.popup.set("mention");
      this.popupIndex.set(0);
      return;
    }
    this.popup.set("none");
  }

  private autosize(el: HTMLTextAreaElement): void {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  }

  private restoreDraft(): void {
    const el = this.inputRef?.nativeElement;
    if (el && !el.value && this.a.draft()) {
      el.value = this.a.draft();
      this.autosize(el);
    }
  }

  /** Index of the last user turn (only it gets the Edit action). */
  lastUserIndex(): number {
    const roles = this.a.turns().map((t) => t.role);
    return roles.lastIndexOf("user");
  }

  /** Pull the last user message back into the input for editing. */
  editLast(): void {
    const text = this.a.editLast();
    if (text === null) return;
    const el = this.inputRef?.nativeElement;
    if (el) {
      el.value = text;
      this.autosize(el);
      el.focus();
    }
  }

  copyTurn(index: number, text: string): void {
    navigator.clipboard?.writeText(text).then(() => {
      this.copiedIdx.set(index);
      setTimeout(() => this.copiedIdx.set(-1), 1500);
    });
  }

  /** Lazily fetch before/after snapshots the first time a change is expanded. */
  onChangeExpand(ev: Event): void {
    const el = ev.target as HTMLDetailsElement;
    if (el.open && Object.keys(this.a.changeDetails()).length === 0) {
      this.a.loadChangeDetails();
    }
  }

  /** How many plan actions are still awaiting an Apply/Skip decision. */
  pendingCount(turn: { plan?: { state: string }[] }): number {
    return (turn.plan ?? []).filter((p) => p.state === "pending").length;
  }

  /** Compact JSON for a diff pane; null means the resource doesn't exist. */
  snapshot(value: unknown): string {
    if (value === null || value === undefined) return "(does not exist)";
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }

  /** Delegated handler for the copy affordance the markdown renderer puts on
   *  fenced code blocks (a span — buttons don't survive innerHTML sanitizing). */
  onMdClick(ev: Event): void {
    const target = ev.target as HTMLElement;
    if (!target?.classList?.contains("md-copy")) return;
    const code =
      target.closest(".md-pre")?.querySelector("code")?.textContent ?? "";
    navigator.clipboard?.writeText(code);
    target.textContent = "copied";
    setTimeout(() => (target.textContent = "copy"), 1500);
  }

  private scrollToBottom(): void {
    const el = this.scroll?.nativeElement;
    if (el) el.scrollTop = el.scrollHeight;
  }

  relativeTime(ts: number): string {
    const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
    if (s < 60) return "just now";
    const m = Math.round(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    const d = Math.round(h / 24);
    return d === 1 ? "yesterday" : `${d}d ago`;
  }
}

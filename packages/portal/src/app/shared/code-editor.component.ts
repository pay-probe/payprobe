import {
  AfterViewInit,
  Component,
  ElementRef,
  EventEmitter,
  Input,
  OnChanges,
  OnDestroy,
  Output,
  SimpleChanges,
  ViewChild,
  effect,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";

import {
  CodeProblem,
  ExecuteNodeResult,
  RunApiService,
} from "../run-monitor/run-api.service";
import { ThemeService } from "./theme.service";
import { MonacoLoaderService } from "./monaco-loader.service";
import {
  CodeHints,
  registerCodeCompletions,
  setModelHints,
} from "./code-completions.model";

type Lang = "python" | "typescript" | "javascript" | "json";

const MIN_HEIGHT = 120;
const MAX_HEIGHT = 1200;

/**
 * A Monaco-backed code editor with syntax highlighting and live, backend-driven
 * syntax validation (Python / JavaScript / TypeScript). Drop-in for a textarea:
 * `[value]` + `(valueChange)`. Errors from the orchestrator's /code/validate are
 * surfaced both as red squiggles (model markers) and a status line.
 *
 * Expandable two ways: drag the grip under the editor to change its height, or
 * press ⤢ to open it full screen (Esc or ⤡ to return; the same editor instance
 * moves, so undo history and cursor survive). With `[hints]` it completes the
 * code step's `inputs` / `context` names; with `runnable` it shows a Run button
 * (also Ctrl/Cmd+Enter) and the last `[result]` beside the code when expanded.
 *
 * Do not wrap it in a `<label>`: the toolbar buttons would become the label's
 * control and fire on any click on the caption.
 */
@Component({
  selector: "app-code-editor",
  standalone: true,
  imports: [],
  template: `
    <div
      #shell
      class="ce-shell"
      [class.ce-shell--expanded]="expanded()"
      (keydown)="onShellKeydown($event)"
    >
      @if (expanded()) {
        <div class="ce-backdrop" (click)="toggleExpanded()"></div>
      }
      <div
        class="ce"
        [class.ce--expanded]="expanded()"
        role="group"
        [attr.aria-label]="title || 'Code editor'"
      >
        <div class="ce__bar">
          <span class="ce__lang">{{ language }}</span>
          @if (expanded() && title) {
            <span class="ce__title">{{ title }}</span>
          }
          <span class="ce__spacer"></span>
          @if (ready()) {
            <span class="ce__pos"
              >Ln {{ cursor().line }}, Col {{ cursor().col }}</span
            >
          }
          @if (runnable) {
            <button
              type="button"
              class="ce__btn ce__btn--run"
              [disabled]="running || !ready()"
              (click)="run.emit()"
              title="Run this step with the upstream results (Ctrl/Cmd+Enter)"
            >
              {{ running ? "Running…" : "▶ Run" }}
            </button>
          }
          <button
            type="button"
            class="ce__btn"
            [class.ce__btn--on]="wrap()"
            (click)="toggleWrap()"
            title="Wrap long lines"
          >
            ↵ Wrap
          </button>
          <button
            type="button"
            class="ce__btn"
            (click)="copy()"
            title="Copy the code"
          >
            {{ copied() ? "✓ Copied" : "Copy" }}
          </button>
          <button
            type="button"
            class="ce__btn"
            (click)="toggleExpanded()"
            [title]="expanded() ? 'Exit full screen (Esc)' : 'Open full screen'"
          >
            {{ expanded() ? "⤡ Collapse" : "⤢ Expand" }}
          </button>
        </div>

        <div class="ce__body">
          <div
            #host
            class="ce__host"
            [style.height.px]="expanded() ? null : inlineHeight()"
          ></div>
          @if (expanded() && runnable) {
            <aside class="ce__out">
              <div class="ce__out-head">
                Last run
                @if (result; as r) {
                  <span
                    class="ce__chip"
                    [class.ce__chip--ok]="r.status === 'passed'"
                  >
                    {{ r.status }}
                  </span>
                  @if (r.duration_ms !== undefined) {
                    <span class="muted">{{ r.duration_ms }} ms</span>
                  }
                }
              </div>
              @if (result; as r) {
                @if (r.error) {
                  <div class="ce__out-label">Error</div>
                  <pre class="ce__pre ce__pre--bad">{{ r.error }}</pre>
                }
                <div class="ce__out-label">Response</div>
                <pre class="ce__pre">{{ asJson(r.response) }}</pre>
                @if (r.logs) {
                  <div class="ce__out-label">print / console output</div>
                  <pre class="ce__pre">{{ r.logs }}</pre>
                }
              } @else {
                <p class="muted ce__out-empty">
                  Not run yet. ▶ Run executes this step once with the upstream
                  steps' last results as <code>context</code>; nothing is saved.
                </p>
              }
            </aside>
          }
        </div>

        @if (!expanded()) {
          <div
            class="ce__grip"
            title="Drag to resize"
            (pointerdown)="startResize($event)"
          ></div>
        }

        <div class="ce__status">
          @if (!ready()) {
            <span class="muted">loading editor…</span>
          } @else if (validating()) {
            <span class="muted">checking syntax…</span>
          } @else if (problems().length) {
            <span class="ce__bad" (click)="goToFirst()">
              ⚠ {{ problems().length }} syntax
              {{ problems().length === 1 ? "error" : "errors" }} —
              {{ problems()[0].message }} (line {{ problems()[0].line }})
            </span>
          } @else if (language === "json") {
            <span class="muted">JSON</span>
          } @else {
            <span class="ce__ok">✓ no syntax errors</span>
          }
          @if (hints && language !== "json") {
            <span class="ce__hint muted">
              Ctrl+Space: <code>inputs</code>, <code>context</code>, snippets
            </span>
          }
        </div>
      </div>
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      .ce {
        border: 1px solid var(--color-border);
        border-radius: 8px;
        overflow: hidden;
        background: var(--color-node);
        display: flex;
        flex-direction: column;
      }
      .ce__bar {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 4px 6px 4px 10px;
        border-bottom: 1px solid var(--color-border);
        background: var(--color-bg);
        font-size: 11px;
        min-width: 0;
        flex-wrap: wrap; /* narrow side panels: buttons wrap, never clip */
      }
      .ce__lang {
        text-transform: uppercase;
        letter-spacing: 0.04em;
        font-weight: 600;
        color: var(--color-muted);
      }
      .ce__title {
        font-family: var(--font-mono, monospace);
        color: var(--color-text);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .ce__spacer {
        flex: 1;
      }
      .ce__pos {
        color: var(--color-muted);
        font-family: var(--font-mono, monospace);
        white-space: nowrap;
      }
      .ce__btn {
        border: 1px solid transparent;
        background: transparent;
        color: var(--color-muted);
        border-radius: 5px;
        padding: 2px 7px;
        font-size: 11px;
        cursor: pointer;
        white-space: nowrap;
      }
      .ce__btn:hover:not(:disabled) {
        color: var(--color-text);
        border-color: var(--color-border);
      }
      .ce__btn:focus-visible {
        outline: 2px solid var(--color-primary);
        outline-offset: 1px;
      }
      .ce__btn--on {
        color: var(--color-primary);
      }
      .ce__btn--run {
        color: var(--color-primary);
        border-color: color-mix(in srgb, var(--color-primary) 40%, transparent);
      }
      .ce__btn:disabled {
        opacity: 0.5;
        cursor: default;
      }
      .ce__body {
        display: flex;
        min-height: 0;
      }
      .ce__host {
        width: 100%;
        min-width: 0;
      }
      .ce__grip {
        height: 8px;
        cursor: ns-resize;
        border-top: 1px solid var(--color-border);
        background: var(--color-bg);
        display: flex;
        align-items: center;
        justify-content: center;
        touch-action: none;
      }
      .ce__grip::after {
        content: "";
        width: 36px;
        height: 3px;
        border-radius: 2px;
        background: var(--color-muted);
        opacity: 0.5;
      }
      .ce__grip:hover::after {
        opacity: 1;
        background: var(--color-primary);
      }
      .ce__status {
        display: flex;
        gap: 12px;
        align-items: center;
        font-size: 11px;
        padding: 4px 10px;
        border-top: 1px solid var(--color-border);
        background: var(--color-bg);
      }
      .ce__hint {
        margin-left: auto;
        white-space: nowrap;
      }
      .ce__ok {
        color: var(--color-pass, #3fb950);
      }
      .ce__bad {
        color: var(--color-fail);
        cursor: pointer;
      }
      .muted {
        color: var(--color-muted);
      }
      code {
        font-family: var(--font-mono, monospace);
      }

      /* -- full screen -------------------------------------------------- */
      .ce-backdrop {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.45);
        z-index: 1150; /* above sticky chrome, below modals and toasts */
      }
      .ce--expanded {
        position: fixed;
        inset: 32px;
        z-index: 1151;
        box-shadow: 0 18px 60px rgba(0, 0, 0, 0.45);
      }
      .ce--expanded .ce__bar {
        padding: 8px 10px 8px 14px;
        font-size: 12px;
      }
      .ce--expanded .ce__body {
        flex: 1;
      }
      .ce--expanded .ce__host {
        height: auto;
        flex: 1;
      }
      .ce__out {
        width: 38%;
        min-width: 260px;
        max-width: 560px;
        border-left: 1px solid var(--color-border);
        background: var(--color-bg);
        overflow: auto;
        padding: 10px 12px;
        font-size: 12px;
      }
      .ce__out-head {
        display: flex;
        gap: 8px;
        align-items: center;
        font-weight: 600;
        margin-bottom: 8px;
      }
      .ce__out-label {
        color: var(--color-muted);
        font-size: 11px;
        margin: 10px 0 4px;
      }
      .ce__out-empty {
        line-height: 1.5;
      }
      .ce__chip {
        font-size: 11px;
        font-weight: 600;
        padding: 1px 8px;
        border-radius: 10px;
        color: var(--color-fail);
        background: color-mix(in srgb, var(--color-fail) 14%, transparent);
      }
      .ce__chip--ok {
        color: var(--color-pass, #3fb950);
        background: color-mix(
          in srgb,
          var(--color-pass, #3fb950) 14%,
          transparent
        );
      }
      .ce__pre {
        margin: 0;
        padding: 8px;
        border: 1px solid var(--color-border);
        border-radius: 6px;
        background: var(--color-node);
        font-family: var(--font-mono, monospace);
        font-size: 11.5px;
        white-space: pre-wrap;
        word-break: break-word;
      }
      .ce__pre--bad {
        color: var(--color-fail);
      }
      @media (max-width: 720px) {
        .ce--expanded {
          inset: 8px;
        }
        .ce--expanded .ce__body {
          flex-direction: column;
        }
        .ce__out {
          width: auto;
          max-width: none;
          max-height: 40%;
          border-left: 0;
          border-top: 1px solid var(--color-border);
        }
      }
    `,
  ],
})
export class CodeEditorComponent
  implements AfterViewInit, OnChanges, OnDestroy
{
  @Input() value = "";
  @Input() language: Lang = "python";
  @Input() height = 240;
  /** Shown in the toolbar when full screen (e.g. "step_004 · code"). */
  @Input() title = "";
  /** Code-step contract names to complete; null = no custom completions. */
  @Input() hints: CodeHints | null = null;
  /** Show ▶ Run (and Ctrl/Cmd+Enter); the host performs the run. */
  @Input() runnable = false;
  @Input() running = false;
  /** Last run of this code, shown beside the editor when full screen. */
  @Input() result: ExecuteNodeResult | null = null;
  @Output() valueChange = new EventEmitter<string>();
  @Output() run = new EventEmitter<void>();

  @ViewChild("host", { static: true }) host!: ElementRef<HTMLElement>;
  @ViewChild("shell", { static: true }) shell!: ElementRef<HTMLElement>;

  private readonly loader = inject(MonacoLoaderService);
  private readonly theme = inject(ThemeService);
  private readonly runApi = inject(RunApiService);

  readonly ready = signal(false);
  readonly validating = signal(false);
  readonly problems = signal<CodeProblem[]>([]);
  readonly expanded = signal(false);
  readonly wrap = signal(false);
  readonly copied = signal(false);
  readonly cursor = signal({ line: 1, col: 1 });
  readonly inlineHeight = signal(240);

  private monaco: any;
  private editor: any;
  private model: any;
  private applyingExternal = false;
  private debounce: ReturnType<typeof setTimeout> | null = null;
  private copiedTimer: ReturnType<typeof setTimeout> | null = null;
  /** Marks where the shell lives inline while it is moved to <body>. */
  private anchor: Comment | null = null;
  private resizeCleanup: (() => void) | null = null;
  /** Monaco context key: Ctrl/Cmd+Enter runs only when the host allows it,
   *  and keeps its default (insert line below) everywhere else. */
  private runnableKey: any;

  constructor() {
    // Keep Monaco's theme in sync with the app theme.
    effect(() => {
      const dark = this.theme.theme() === "dark";
      if (this.monaco) this.monaco.editor.setTheme(dark ? "vs-dark" : "vs");
    });
  }

  async ngAfterViewInit(): Promise<void> {
    try {
      this.monaco = await this.loader.load();
    } catch {
      return; // loader failure: the textarea-less host stays empty but harmless
    }
    registerCodeCompletions(this.monaco);
    this.monaco.editor.setTheme(
      this.theme.theme() === "dark" ? "vs-dark" : "vs",
    );
    this.editor = this.monaco.editor.create(this.host.nativeElement, {
      value: this.value ?? "",
      language: this.language,
      automaticLayout: true,
      minimap: { enabled: false },
      fontSize: 13,
      fontFamily: '"JetBrains Mono", ui-monospace, Menlo, Consolas, monospace',
      lineNumbers: "on",
      tabSize: this.tabSize(),
      insertSpaces: true,
      detectIndentation: false,
      scrollBeyondLastLine: false,
      renderLineHighlight: "line",
      renderWhitespace: "boundary",
      bracketPairColorization: { enabled: true },
      guides: { bracketPairs: "active", indentation: true },
      matchBrackets: "always",
      autoClosingBrackets: "languageDefined",
      autoClosingQuotes: "languageDefined",
      suggestOnTriggerCharacters: true,
      quickSuggestions: { other: true, comments: false, strings: false },
      smoothScrolling: true,
      wordWrap: "off",
      fixedOverflowWidgets: true,
      padding: { top: 8, bottom: 8 },
    });
    this.model = this.editor.getModel();
    setModelHints(this.model.uri.toString(), this.hints);
    this.editor.onDidChangeModelContent(() => {
      if (this.applyingExternal) return;
      const v = this.editor.getValue();
      this.value = v;
      this.valueChange.emit(v);
      this.scheduleValidate();
    });
    this.editor.onDidChangeCursorPosition((e: any) =>
      this.cursor.set({ line: e.position.lineNumber, col: e.position.column }),
    );
    this.runnableKey = this.editor.createContextKey(
      "ppRunnable",
      this.runnable,
    );
    this.editor.addCommand(
      this.monaco.KeyMod.CtrlCmd | this.monaco.KeyCode.Enter,
      () => {
        if (!this.running) this.run.emit();
      },
      "ppRunnable",
    );
    this.ready.set(true);
    this.scheduleValidate();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes["height"]) this.inlineHeight.set(this.clamp(this.height));
    if (!this.editor) return;
    if (changes["value"] && this.editor.getValue() !== (this.value ?? "")) {
      this.applyingExternal = true;
      this.editor.setValue(this.value ?? "");
      this.applyingExternal = false;
    }
    if (changes["language"]) {
      this.monaco.editor.setModelLanguage(this.model, this.language);
      this.model.updateOptions({ tabSize: this.tabSize() });
      this.scheduleValidate();
    }
    if (changes["hints"]) setModelHints(this.model.uri.toString(), this.hints);
    if (changes["runnable"]) this.runnableKey?.set(this.runnable);
  }

  // -- toolbar ---------------------------------------------------------------

  toggleExpanded(): void {
    const shell = this.shell.nativeElement;
    if (!this.expanded()) {
      // Move to <body> so no transformed/clipping ancestor (side panel,
      // detail window) can trap the fixed overlay.
      this.anchor = document.createComment("code-editor");
      shell.parentNode?.insertBefore(this.anchor, shell);
      document.body.appendChild(shell);
      this.expanded.set(true);
    } else {
      this.anchor?.parentNode?.insertBefore(shell, this.anchor);
      this.anchor?.remove();
      this.anchor = null;
      this.expanded.set(false);
    }
    setTimeout(() => {
      this.editor?.layout();
      this.editor?.focus();
    });
  }

  /** Esc leaves full screen, unless Monaco used it (suggest, find, selection). */
  onShellKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape" || !this.expanded() || event.defaultPrevented) {
      return;
    }
    event.stopPropagation();
    this.toggleExpanded();
  }

  toggleWrap(): void {
    this.wrap.update((w) => !w);
    this.editor?.updateOptions({ wordWrap: this.wrap() ? "on" : "off" });
  }

  copy(): void {
    const text = this.editor?.getValue() ?? this.value ?? "";
    navigator.clipboard?.writeText(text).then(
      () => {
        this.copied.set(true);
        if (this.copiedTimer) clearTimeout(this.copiedTimer);
        this.copiedTimer = setTimeout(() => this.copied.set(false), 1500);
      },
      () => {},
    );
  }

  /** Drag the grip under the editor to change its inline height. */
  startResize(event: PointerEvent): void {
    if (event.button !== 0) return;
    event.preventDefault();
    const startY = event.clientY;
    const startH = this.inlineHeight();
    const move = (e: PointerEvent) =>
      this.inlineHeight.set(this.clamp(startH + e.clientY - startY));
    const up = () => this.resizeCleanup?.();
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    this.resizeCleanup = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      this.resizeCleanup = null;
    };
  }

  asJson(value: unknown): string {
    try {
      return JSON.stringify(value ?? {}, null, 2);
    } catch {
      return String(value);
    }
  }

  // -- validation ------------------------------------------------------------

  private scheduleValidate(): void {
    if (this.debounce) clearTimeout(this.debounce);
    this.debounce = setTimeout(() => this.runValidation(), 450);
  }

  private runValidation(): void {
    if (!this.editor) return;
    // JSON is validated by Monaco itself; only python/js/ts use the backend.
    if (this.language === "json") {
      this.validating.set(false);
      this.problems.set([]);
      return;
    }
    const code = this.editor.getValue();
    this.validating.set(true);
    this.runApi.validateCode(this.language, code).subscribe({
      next: (res) => {
        this.validating.set(false);
        const errors = res.errors ?? [];
        this.problems.set(errors);
        const markers = errors.map((e) => ({
          startLineNumber: e.line,
          startColumn: e.column,
          endLineNumber: e.line,
          endColumn: e.column + 1,
          message: e.message,
          severity: this.monaco.MarkerSeverity.Error,
        }));
        this.monaco.editor.setModelMarkers(this.model, "payprobe", markers);
      },
      error: () => this.validating.set(false),
    });
  }

  goToFirst(): void {
    const p = this.problems()[0];
    if (p && this.editor) {
      this.editor.revealLineInCenter(p.line);
      this.editor.setPosition({ lineNumber: p.line, column: p.column });
      this.editor.focus();
    }
  }

  private tabSize(): number {
    return this.language === "python" ? 4 : 2;
  }

  private clamp(h: number): number {
    return Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, Math.round(h)));
  }

  ngOnDestroy(): void {
    if (this.debounce) clearTimeout(this.debounce);
    if (this.copiedTimer) clearTimeout(this.copiedTimer);
    this.resizeCleanup?.();
    // Expanded shells live under <body>, where Angular will not remove them.
    if (this.anchor) {
      this.shell.nativeElement.remove();
      this.anchor.remove();
      this.anchor = null;
    }
    if (this.model) setModelHints(this.model.uri.toString(), null);
    this.editor?.dispose();
  }
}

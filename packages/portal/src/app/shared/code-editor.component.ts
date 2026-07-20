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

import { CodeProblem, RunApiService } from "../run-monitor/run-api.service";
import { ThemeService } from "./theme.service";
import { MonacoLoaderService } from "./monaco-loader.service";

type Lang = "python" | "typescript" | "javascript" | "json";

/**
 * A Monaco-backed code editor with syntax highlighting and live, backend-driven
 * syntax validation (Python / JavaScript / TypeScript). Drop-in for a textarea:
 * `[value]` + `(valueChange)`. Errors from the orchestrator's /code/validate are
 * surfaced both as red squiggles (model markers) and a status line.
 */
@Component({
  selector: "app-code-editor",
  standalone: true,
  imports: [],
  template: `
    <div class="ce">
      <div #host class="ce__host" [style.height.px]="height"></div>
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
      }
      .ce__host {
        width: 100%;
      }
      .ce__status {
        font-size: 11px;
        padding: 4px 10px;
        border-top: 1px solid var(--color-border);
        background: var(--color-bg);
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
    `,
  ],
})
export class CodeEditorComponent
  implements AfterViewInit, OnChanges, OnDestroy
{
  @Input() value = "";
  @Input() language: Lang = "python";
  @Input() height = 240;
  @Output() valueChange = new EventEmitter<string>();

  @ViewChild("host", { static: true }) host!: ElementRef<HTMLElement>;

  private readonly loader = inject(MonacoLoaderService);
  private readonly theme = inject(ThemeService);
  private readonly runApi = inject(RunApiService);

  readonly ready = signal(false);
  readonly validating = signal(false);
  readonly problems = signal<CodeProblem[]>([]);

  private monaco: any;
  private editor: any;
  private model: any;
  private applyingExternal = false;
  private debounce: ReturnType<typeof setTimeout> | null = null;

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
    this.monaco.editor.setTheme(
      this.theme.theme() === "dark" ? "vs-dark" : "vs",
    );
    this.editor = this.monaco.editor.create(this.host.nativeElement, {
      value: this.value ?? "",
      language: this.language,
      automaticLayout: true,
      minimap: { enabled: false },
      fontSize: 13,
      lineNumbers: "on",
      tabSize: 4,
      scrollBeyondLastLine: false,
      renderLineHighlight: "line",
      fixedOverflowWidgets: true,
      padding: { top: 8, bottom: 8 },
    });
    this.model = this.editor.getModel();
    this.editor.onDidChangeModelContent(() => {
      if (this.applyingExternal) return;
      const v = this.editor.getValue();
      this.value = v;
      this.valueChange.emit(v);
      this.scheduleValidate();
    });
    this.ready.set(true);
    this.scheduleValidate();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (!this.editor) return;
    if (changes["value"] && this.editor.getValue() !== (this.value ?? "")) {
      this.applyingExternal = true;
      this.editor.setValue(this.value ?? "");
      this.applyingExternal = false;
    }
    if (changes["language"]) {
      this.monaco.editor.setModelLanguage(this.model, this.language);
      this.scheduleValidate();
    }
  }

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

  ngOnDestroy(): void {
    if (this.debounce) clearTimeout(this.debounce);
    this.editor?.dispose();
  }
}

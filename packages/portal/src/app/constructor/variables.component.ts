import {
  Component,
  OnInit,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";
import { RouterLink } from "@angular/router";

import { ThemeService } from "../shared/theme.service";
import { UiService } from "../shared/ui.service";
import { ScenarioApiService } from "./scenario-api.service";
import { Project, ScenarioSet } from "./scenario.models";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

type Scope = "global" | "project" | "set";
interface Row {
  key: string;
  value: string;
  secret?: boolean;
}

/**
 * Variables manager — edit Global, Project-level and Set-level variables. At run
 * time these merge under a scenario's own variables (scenario wins) and are
 * available in steps as ${vars.NAME}.
 */
@Component({
  selector: "app-variables",
  standalone: true,
  imports: [PageHeaderComponent, FormsModule, RouterLink],
  templateUrl: "./variables.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./variables.component.scss",
})
export class VariablesComponent implements OnInit {
  readonly theme = inject(ThemeService);
  private readonly api = inject(ScenarioApiService);
  private readonly ui = inject(UiService);

  readonly scope = signal<Scope>("global");
  readonly projects = signal<Project[]>([]);
  readonly sets = signal<ScenarioSet[]>([]);
  readonly projectId = signal<string>("");
  readonly setId = signal<string>("");
  readonly rows = signal<Row[]>([]);
  readonly saving = signal(false);
  readonly loading = signal(false);

  readonly scopes: { value: Scope; label: string }[] = [
    { value: "global", label: "Global" },
    { value: "project", label: "Project" },
    { value: "set", label: "Set" },
  ];

  ngOnInit(): void {
    this.api.projects().subscribe({
      next: (p) => this.projects.set(p),
      error: () => this.projects.set([]),
    });
    this.loadGlobal();
  }

  setScope(s: Scope): void {
    this.scope.set(s);
    this.rows.set([]);
    if (s === "global") this.loadGlobal();
  }

  onProjectChange(id: string): void {
    this.projectId.set(id);
    if (this.scope() === "project") this.loadScope("project", id);
    if (this.scope() === "set") {
      this.api.sets(id).subscribe({
        next: (s) => this.sets.set(s),
        error: () => this.sets.set([]),
      });
      this.setId.set("");
      this.rows.set([]);
    }
  }

  onSetChange(id: string): void {
    this.setId.set(id);
    this.loadScope("set", id);
  }

  private toRows(vars: Record<string, unknown>, secrets: string[] = []): Row[] {
    const sset = new Set(secrets);
    return Object.entries(vars).map(([key, v]) => ({
      key,
      value: typeof v === "string" ? v : JSON.stringify(v),
      secret: sset.has(key),
    }));
  }

  toggleSecret(i: number): void {
    this.rows.update((r) =>
      r.map((x, j) => (j === i ? { ...x, secret: !x.secret } : x)),
    );
  }

  private secretsFromRows(): string[] {
    return this.rows()
      .filter((r) => r.secret && r.key.trim())
      .map((r) => r.key.trim());
  }

  private fromRows(): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const r of this.rows()) {
      if (!r.key.trim()) continue;
      try {
        out[r.key.trim()] = JSON.parse(r.value);
      } catch {
        out[r.key.trim()] = r.value;
      }
    }
    return out;
  }

  private loadGlobal(): void {
    this.loading.set(true);
    this.api.globalVariables().subscribe({
      next: (r) => {
        this.rows.set(this.toRows(r.variables, r.secrets));
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  private loadScope(scope: "project" | "set", id: string): void {
    if (!id) return;
    this.loading.set(true);
    this.api.scopeVariables(scope, id).subscribe({
      next: (r) => {
        this.rows.set(this.toRows(r.variables, r.secrets));
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  addRow(): void {
    this.rows.update((r) => [...r, { key: "", value: "" }]);
  }
  removeRow(i: number): void {
    this.rows.update((r) => r.filter((_, j) => j !== i));
  }
  updateRow(i: number, patch: Partial<Row>): void {
    this.rows.update((r) =>
      r.map((x, j) => (j === i ? { ...x, ...patch } : x)),
    );
  }

  canEdit(): boolean {
    if (this.scope() === "global") return true;
    if (this.scope() === "project") return !!this.projectId();
    return !!this.setId();
  }

  save(): void {
    const vars = this.fromRows();
    this.saving.set(true);
    const done = {
      next: () => {
        this.saving.set(false);
        this.ui.toast("success", "Variables saved.");
      },
      error: () => {
        this.saving.set(false);
        this.ui.toast("error", "Save failed.");
      },
    };
    const secrets = this.secretsFromRows();
    if (this.scope() === "global")
      this.api.setGlobalVariables(vars, secrets).subscribe(done);
    else if (this.scope() === "project")
      this.api
        .setScopeVariables("project", this.projectId(), vars, secrets)
        .subscribe(done);
    else
      this.api
        .setScopeVariables("set", this.setId(), vars, secrets)
        .subscribe(done);
  }
}

import { CommonModule, DatePipe } from "@angular/common";
import {
  Component,
  ElementRef,
  HostListener,
  OnInit,
  ViewChild,
  DestroyRef,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";
import { takeUntilDestroyed } from "@angular/core/rxjs-interop";
import { ActivatedRoute, ParamMap, Router, RouterLink } from "@angular/router";

import { ThemeService } from "../shared/theme.service";
import { UiService } from "../shared/ui.service";
import { ScenarioApiService } from "./scenario-api.service";
import {
  CATEGORY_PRESETS,
  DEFAULT_PROJECT_ID,
  Project,
  ScenarioSet,
  ScenarioSummary,
  SearchResults,
  TEST_CLASSES,
  TestClass,
  categoryPreset,
} from "./scenario.models";
import { RunApiService } from "../run-monitor/run-api.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { IconComponent } from "../shared/ui/icon.component";

/** `?set=` value for the "All cases" view of a project. */
const ALL_CASES = "all";

/** The list's place in the URL: `/constructor` = projects,
 *  `?project=P` = that project's sets, `?project=P&set=S|all` = the cases,
 *  `&class=C` = the test-class filter. */
interface ListLocation {
  project?: string | null;
  set?: string | null;
  class?: string | null;
}

@Component({
  selector: "app-scenario-list",
  standalone: true,
  imports: [
    PageHeaderComponent,
    CommonModule,
    DatePipe,
    FormsModule,
    RouterLink,
    IconComponent,
  ],
  templateUrl: "./scenario-list.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./scenario-list.component.scss",
})
export class ScenarioListComponent implements OnInit {
  readonly theme = inject(ThemeService);

  private readonly api = inject(ScenarioApiService);
  private readonly runApi = inject(RunApiService);
  private readonly ui = inject(UiService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  private readonly destroyRef = inject(DestroyRef);

  @ViewChild("importInput")
  importInput?: ElementRef<HTMLInputElement>;

  readonly testClasses = TEST_CLASSES;
  readonly defaultProjectId = DEFAULT_PROJECT_ID;

  // -- organisation state ----------------------------------------------------
  readonly projects = signal<Project[] | null>(null);
  readonly selectedProjectId = signal<string>(DEFAULT_PROJECT_ID);
  readonly sets = signal<ScenarioSet[]>([]);
  readonly selectedSetId = signal<string | null>(null);
  readonly classFilter = signal<TestClass | null>(null);

  // -- staged navigation: projects -> sets -> scenarios ----------------------
  readonly stage = signal<"projects" | "sets" | "scenarios">("projects");

  readonly scenarios = signal<ScenarioSummary[] | null>(null);
  readonly error = signal<string | null>(null);
  readonly importing = signal(false);

  readonly selectedProject = computed(
    () =>
      this.projects()?.find((p) => p.id === this.selectedProjectId()) ?? null,
  );

  // -- inline create forms -----------------------------------------------------
  readonly newProjectOpen = signal(false);
  readonly newSetOpen = signal(false);
  newSetName = "";

  // -- project create / edit form ----------------------------------------------
  readonly categoryPresets = CATEGORY_PRESETS;
  /** null id = creating a new project; otherwise editing that project. */
  readonly projectFormId = signal<string | null>(null);
  pfName = "";
  pfDescription = "";
  pfCategory = "";

  /** The logo/color for the currently-picked category (for the form preview). */
  pfPreset(): { icon: string; color: string } | null {
    const p = categoryPreset(this.pfCategory);
    return p ? { icon: p.icon, color: p.color } : null;
  }

  // -- per-card "sets" menu ----------------------------------------------------
  readonly setMenuFor = signal<string | null>(null);

  // -- global search -----------------------------------------------------------
  readonly searchResults = signal<SearchResults | null>(null);
  readonly searchOpen = signal(false);
  searchText = "";
  private searchTimer: ReturnType<typeof setTimeout> | null = null;

  ngOnInit(): void {
    // The URL is the single source of truth for project / set / class, so
    // refresh, deep links and browser back/forward all land on the same view.
    this.route.queryParamMap
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((q) => this.applyUrl(q));
    this.reloadProjects();
  }

  /** Apply the query params to the view state and reload what changed. */
  private applyUrl(q: ParamMap): void {
    const project = q.get("project");
    const set = q.get("set");
    const cls = q.get("class");
    const classFilter = (TEST_CLASSES as readonly string[]).includes(cls ?? "")
      ? (cls as TestClass)
      : null;
    const setId = set && set !== ALL_CASES ? set : null;
    // On the projects stage keep the last project, so the list stays warm.
    const projectId = project ?? this.selectedProjectId();
    const projectChanged = projectId !== this.selectedProjectId();
    const casesChanged =
      projectChanged ||
      setId !== this.selectedSetId() ||
      classFilter !== this.classFilter();

    this.selectedProjectId.set(projectId);
    this.selectedSetId.set(setId);
    this.classFilter.set(classFilter);
    this.stage.set(!project ? "projects" : set ? "scenarios" : "sets");
    this.setMenuFor.set(null);

    // First load still in flight: reloadProjects() picks this state up.
    if (this.projects() === null) return;
    if (projectChanged) this.reloadSets();
    if (casesChanged) this.reloadScenarios();
  }

  /** Move the list to a new place; the URL subscription applies it. */
  private go(to: ListLocation, replaceUrl = false): void {
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: {
        project: to.project ?? null,
        set: to.set ?? null,
        class: to.class ?? null,
      },
      replaceUrl,
    });
  }

  /** Query params that bring the editor's back button to this exact view. */
  private caseContext(): ListLocation {
    return {
      project: this.selectedProjectId(),
      set: this.selectedSetId(),
      class: this.classFilter(),
    };
  }

  @HostListener("document:keydown.escape")
  onEscape(): void {
    this.searchOpen.set(false);
    this.setMenuFor.set(null);
  }

  // -- loading -----------------------------------------------------------------

  reloadProjects(): void {
    this.error.set(null);
    this.api.projects().subscribe({
      next: (list) => {
        this.projects.set(list);
        if (!list.some((p) => p.id === this.selectedProjectId())) {
          this.selectedProjectId.set(
            list.find((p) => p.id === DEFAULT_PROJECT_ID)?.id ??
              list[0]?.id ??
              DEFAULT_PROJECT_ID,
          );
          // The URL named a project that does not exist (stale deep link or
          // deleted): show the projects list rather than a different project.
          if (this.stage() !== "projects") this.go({}, true);
        }
        this.reloadSets();
        this.reloadScenarios();
      },
      error: () =>
        this.error.set(
          "Could not reach the scenario service. Start it with: " +
            "uvicorn api.main:app --port 8000 (in packages/scenario-service)",
        ),
    });
  }

  reloadSets(): void {
    this.api.sets(this.selectedProjectId()).subscribe({
      next: (sets) => {
        this.sets.set(sets);
        if (
          this.selectedSetId() &&
          !sets.some((s) => s.id === this.selectedSetId())
        ) {
          // Unknown set in the URL: fall back to "All cases" and say so in it.
          this.selectedSetId.set(null);
          this.reloadScenarios();
          if (this.stage() === "scenarios") {
            this.go({ ...this.caseContext(), set: ALL_CASES }, true);
          }
        }
      },
      error: () => this.sets.set([]),
    });
  }

  reloadScenarios(): void {
    this.scenarios.set(null);
    this.api
      .list({
        projectId: this.selectedProjectId(),
        setId: this.selectedSetId() ?? undefined,
        testClass: this.classFilter() ?? undefined,
      })
      .subscribe({
        next: (list) => this.scenarios.set(list),
        error: () => {
          this.scenarios.set([]);
          this.error.set("Failed to load test cases.");
        },
      });
  }

  // -- stage navigation (each step is a URL, so back/forward walk it) --------

  /** Drill from the projects list into a project's sets. */
  openProject(p: Project): void {
    this.go({ project: p.id });
  }

  /** Drill from sets into the cases of a set (null = all cases in project). */
  openSet(id: string | null): void {
    this.go({ project: this.selectedProjectId(), set: id ?? ALL_CASES });
  }

  backToProjects(): void {
    this.go({});
  }

  backToSets(): void {
    this.go({ project: this.selectedProjectId() });
  }

  /** Heading for the current scenarios view. */
  currentSetName(): string {
    const id = this.selectedSetId();
    return id ? this.setName(id) : "All cases";
  }

  /** A filter, not a drill-down: replace the history entry. */
  setClass(c: TestClass | null): void {
    this.go(
      {
        project: this.selectedProjectId(),
        set: this.selectedSetId() ?? ALL_CASES,
        class: c,
      },
      true,
    );
  }

  // -- projects CRUD ----------------------------------------------------------------

  /** Open the inline form to create a new project. */
  openNewProject(): void {
    const opening = !this.newProjectOpen();
    if (opening) {
      this.projectFormId.set(null);
      this.pfName = "";
      this.pfDescription = "";
      this.pfCategory = "";
    }
    this.newProjectOpen.set(opening);
  }

  /** Open the inline form pre-filled to edit an existing project. */
  openEditProject(p: Project, event: Event): void {
    event.stopPropagation();
    this.projectFormId.set(p.id);
    this.pfName = p.name;
    this.pfDescription = p.description ?? "";
    this.pfCategory = p.category ?? "";
    this.newProjectOpen.set(true);
  }

  cancelProjectForm(): void {
    this.newProjectOpen.set(false);
    this.projectFormId.set(null);
  }

  /** Create or update a project, stamping the picked category's logo + color. */
  saveProjectForm(): void {
    const name = this.pfName.trim();
    if (!name) return;
    const preset = categoryPreset(this.pfCategory);
    const draft = {
      name,
      description: this.pfDescription.trim(),
      category: this.pfCategory.trim(),
      color: preset?.color ?? "",
      icon: preset?.icon ?? "",
    };
    const id = this.projectFormId();
    const req = id
      ? this.api.updateProject(id, draft)
      : this.api.createProject(draft);
    req.subscribe({
      next: (p) => {
        this.newProjectOpen.set(false);
        this.projectFormId.set(null);
        this.ui.toast(
          "success",
          `Project "${p.name}" ${id ? "updated" : "created"}`,
        );
        this.reloadProjects();
        if (!id) this.go({ project: p.id });
      },
      error: () =>
        this.ui.toast(
          "error",
          `Failed to ${id ? "update" : "create"} project.`,
        ),
    });
  }

  /** The effective logo + color for a project row (stored, else derived). */
  projectBadge(p: Project): { icon: string; color: string } | null {
    if (p.icon || p.color) {
      return { icon: p.icon ?? "layers", color: p.color || "#8b949e" };
    }
    const preset = categoryPreset(p.category);
    return preset ? { icon: preset.icon, color: preset.color } : null;
  }

  /** The effective logo + color for a scenario row (stored, else derived). */
  scenarioBadge(s: ScenarioSummary): { icon: string; color: string } | null {
    if (s.icon || s.color) {
      return { icon: s.icon ?? "layers", color: s.color || "#8b949e" };
    }
    const preset = categoryPreset(s.category);
    return preset ? { icon: preset.icon, color: preset.color } : null;
  }

  async removeProject(p: Project, event: Event): Promise<void> {
    event.stopPropagation();
    const ok = await this.ui.confirm(
      `Delete project "${p.name}"? Its named sets are deleted too. ` +
        "Projects that still contain test cases cannot be deleted.",
      { title: "Delete project", confirmLabel: "Delete", danger: true },
    );
    if (!ok) return;
    this.api.deleteProject(p.id).subscribe({
      next: () => {
        this.ui.toast("success", `Deleted project "${p.name}"`);
        if (this.selectedProjectId() === p.id) {
          this.selectedProjectId.set(DEFAULT_PROJECT_ID);
          this.selectedSetId.set(null);
        }
        this.reloadProjects();
      },
      error: (err) =>
        this.ui.toast(
          "error",
          err?.status === 409
            ? "Project still contains test cases — move or delete them first."
            : "Failed to delete project.",
        ),
    });
  }

  // -- sets CRUD ----------------------------------------------------------------------

  createSet(): void {
    const name = this.newSetName.trim();
    if (!name) return;
    this.api.createSet(this.selectedProjectId(), { name }).subscribe({
      next: (s) => {
        this.newSetName = "";
        this.newSetOpen.set(false);
        this.ui.toast("success", `Set "${s.name}" created`);
        this.reloadSets();
      },
      error: () => this.ui.toast("error", "Failed to create set."),
    });
  }

  async removeSet(s: ScenarioSet, event: Event): Promise<void> {
    event.stopPropagation();
    const ok = await this.ui.confirm(
      `Delete set "${s.name}"? The test cases in it are kept.`,
      { title: "Delete set", confirmLabel: "Delete", danger: true },
    );
    if (!ok) return;
    this.api.deleteSet(s.id).subscribe({
      next: () => {
        if (this.selectedSetId() === s.id) this.selectedSetId.set(null);
        this.reloadSets();
        this.reloadScenarios();
      },
      error: () => this.ui.toast("error", "Failed to delete set."),
    });
  }

  // -- card set-membership menu ----------------------------------------------------

  toggleSetMenu(scenarioId: string, event: Event): void {
    event.stopPropagation();
    event.preventDefault();
    this.setMenuFor.update((cur) => (cur === scenarioId ? null : scenarioId));
  }

  isInSet(set: ScenarioSet, scenarioId: string): boolean {
    return set.scenario_ids.includes(scenarioId);
  }

  toggleMembership(set: ScenarioSet, scenarioId: string, event: Event): void {
    event.stopPropagation();
    event.preventDefault();
    const req = this.isInSet(set, scenarioId)
      ? this.api.removeFromSet(set.id, scenarioId)
      : this.api.addToSet(set.id, scenarioId);
    req.subscribe({
      next: (updated) => {
        this.sets.update((list) =>
          list.map((s) => (s.id === updated.id ? updated : s)),
        );
        if (this.selectedSetId() === set.id) this.reloadScenarios();
      },
      error: () => this.ui.toast("error", "Failed to update set membership."),
    });
  }

  membershipCount(scenarioId: string): number {
    return this.sets().filter((s) => s.scenario_ids.includes(scenarioId))
      .length;
  }

  // -- global search -------------------------------------------------------------------

  onSearchInput(text: string): void {
    this.searchText = text;
    if (this.searchTimer) clearTimeout(this.searchTimer);
    const q = text.trim();
    if (!q) {
      this.searchResults.set(null);
      this.searchOpen.set(false);
      return;
    }
    this.searchTimer = setTimeout(() => {
      this.api.search(q).subscribe({
        next: (res) => {
          this.searchResults.set(res);
          this.searchOpen.set(true);
        },
        error: () => this.searchResults.set(null),
      });
    }, 250);
  }

  onSearchBlur(): void {
    // Delay so result clicks land before the dropdown closes.
    setTimeout(() => this.searchOpen.set(false), 200);
  }

  hasResults(res: SearchResults): boolean {
    return (
      res.projects.length > 0 || res.sets.length > 0 || res.scenarios.length > 0
    );
  }

  goToProject(p: Project): void {
    this.searchOpen.set(false);
    this.searchText = "";
    this.go({ project: p.id });
  }

  goToSet(s: ScenarioSet): void {
    this.searchOpen.set(false);
    this.searchText = "";
    this.go({ project: s.project_id, set: s.id });
  }

  /** Search hit: open the case; back returns to its project's cases. */
  goToScenario(s: ScenarioSummary): void {
    this.searchOpen.set(false);
    this.router.navigate(["/constructor", s.id], {
      queryParams: { project: s.project_id },
    });
  }

  /** Row in the cases table: open it; back returns to this exact view. */
  openCase(s: ScenarioSummary): void {
    this.router.navigate(["/constructor", s.id], {
      queryParams: this.caseContext(),
    });
  }

  projectName(id: string): string {
    return this.projects()?.find((p) => p.id === id)?.name ?? id;
  }

  // -- import / delete --------------------------------------------------------------------

  pickImportFile(): void {
    this.importInput?.nativeElement.click();
  }

  async onImportFile(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = ""; // allow re-selecting the same file
    if (!file) return;
    const content = await file.text();
    this.importing.set(true);
    this.api.import(content, this.selectedProjectId()).subscribe({
      next: (sc) => {
        this.importing.set(false);
        this.ui.toast("success", `Imported "${sc.name}"`);
        const setId = this.selectedSetId();
        if (setId) {
          this.api.addToSet(setId, sc.id).subscribe({
            error: () =>
              this.ui.toast(
                "error",
                "Imported, but could not add the case to the set.",
              ),
          });
        }
        this.router.navigate(["/constructor", sc.id], {
          queryParams: { ...this.caseContext(), class: null },
        });
      },
      error: (err) => {
        this.importing.set(false);
        const detail = err?.error?.detail;
        const message = Array.isArray(detail)
          ? `Import rejected: ${detail
              .map((i: { message?: string }) => i.message)
              .filter(Boolean)
              .join("; ")}`
          : typeof detail === "string"
            ? `Import rejected: ${detail}`
            : "Import failed — is the scenario service running?";
        this.ui.toast("error", message);
      },
    });
  }

  duplicate(s: ScenarioSummary, event: Event): void {
    event.stopPropagation();
    event.preventDefault();
    this.api.duplicate(s.id).subscribe({
      next: (copy) => {
        this.ui.toast("success", `Duplicated as "${copy.name}"`);
        this.reloadSets();
        this.reloadScenarios();
      },
      error: () => this.ui.toast("error", `Failed to duplicate "${s.name}".`),
    });
  }

  async remove(s: ScenarioSummary, event: Event): Promise<void> {
    event.stopPropagation();
    event.preventDefault();
    const ok = await this.ui.confirm(
      `Delete scenario "${s.name}"? Past run results that reference it are kept.`,
      { title: "Delete scenario", confirmLabel: "Delete", danger: true },
    );
    if (!ok) return;
    this.api.delete(s.id).subscribe({
      next: () => {
        this.ui.toast("success", `Deleted "${s.name}"`);
        this.reloadSets();
        this.reloadScenarios();
      },
      error: () => this.ui.toast("error", `Failed to delete "${s.name}".`),
    });
  }

  // -- run actions -----------------------------------------------------------

  /** Run a single test case and jump to its live monitor. */
  runOne(s: ScenarioSummary, event: Event): void {
    event.stopPropagation();
    event.preventDefault();
    this.runApi.start({ scenario_ids: [s.id], label: s.name }).subscribe({
      next: (res) => this.router.navigate(["/run-monitor", res.run_id]),
      error: () =>
        this.ui.toast("error", `Could not start a run for "${s.name}".`),
    });
  }

  /** Run every case in the selected project (or the selected set). */
  runProject(): void {
    const setId = this.selectedSetId();
    const req = setId
      ? { set_id: setId, label: this.setName(setId) }
      : {
          project_id: this.selectedProjectId(),
          label: this.selectedProject()?.name ?? "project",
        };
    this.runApi.start(req).subscribe({
      next: (res) => this.router.navigate(["/run-monitor", res.run_id]),
      error: () => this.ui.toast("error", "Could not start the run."),
    });
  }

  private setName(setId: string): string {
    return this.sets().find((x) => x.id === setId)?.name ?? "set";
  }
}

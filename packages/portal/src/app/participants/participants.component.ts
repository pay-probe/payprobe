import {
  Component,
  OnInit,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { Router } from "@angular/router";
import { FormsModule } from "@angular/forms";

import { UiService } from "../shared/ui.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { ParticipantsService, ParticipantFlow } from "./participants.service";

/**
 * Participant flows — listening, reactive stand-ins. This is the manage page:
 * list flows, start/stop them as listeners, and open the visual canvas editor
 * to author one. All editing happens on the canvas (`/flow-editor/:id`).
 */
@Component({
  selector: "app-participants",
  standalone: true,
  imports: [PageHeaderComponent, ButtonComponent, IconComponent, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <pp-page-header
      subtitle="Listening stand-ins: a participant flow answers incoming messages by walking a graph (trigger → logic → reply). Create one on the canvas, then start it as a listener."
    >
      <h1>Participant Flows</h1>
    </pp-page-header>

    <div class="pf">
      <div class="pf__head">
        <span class="spacer"></span>
        <pp-button variant="ghost" size="sm" icon="refresh" (click)="refresh()"
          >Refresh</pp-button
        >
        <pp-button
          variant="primary"
          size="sm"
          icon="plus"
          (click)="openCanvas()"
          >New flow</pp-button
        >
      </div>

      @for (f of svc.flows(); track f.id) {
        <div class="pf-row">
          <button
            class="pf-row__main"
            (click)="openCanvas(f.id)"
            title="Edit on canvas"
          >
            <strong>{{ f.name }}</strong>
            <small class="muted">{{ f.description || f.id }}</small>
          </button>
          @if (svc.runningFor(f.id); as r) {
            <span class="pf-pill pf-pill--on" title="Listening">
              ● {{ r.endpoint || ":" + r.port }} · {{ r.received }} msg
            </span>
            @if (r.owner && r.owner !== "standalone") {
              <span
                class="pf-pill"
                title="Started as part of a topology run — stop the topology to stop it"
              >
                via topology
              </span>
            } @else {
              <pp-button variant="ghost" size="sm" (click)="stop(r.id)"
                >Stop</pp-button
              >
            }
          } @else {
            <pp-button variant="secondary" size="sm" (click)="start(f.id)"
              >Start</pp-button
            >
          }
          <pp-button variant="ghost" size="sm" (click)="startRename(f)"
            >Rename</pp-button
          >
          <button class="pf-del" title="Delete flow" (click)="remove(f)">
            <pp-icon name="trash" [size]="16" />
          </button>
        </div>
      } @empty {
        <p class="muted">No participant flows yet. Create one to begin.</p>
      }
    </div>

    @if (renaming(); as rn) {
      <div class="rn-back" (click)="cancelRename()">
        <div class="rn" (click)="$event.stopPropagation()">
          <h3>Rename / Save As</h3>
          <p class="muted">
            A flow's id is a stable handle, so renaming makes a fresh copy under
            a new name. The id changes; the URL becomes /flow-editor/{{
              "{new id}"
            }}.
          </p>
          <label class="rn-fld">
            <span>New name</span>
            <input
              type="text"
              [(ngModel)]="rn.name"
              (keyup.enter)="doRename()"
            />
          </label>
          <label class="rn-chk">
            <input type="checkbox" [(ngModel)]="rn.replace" />
            <span>
              Replace original — repoint any topology to the copy and delete the
              old flow (a true rename). Unchecked = keep both (duplicate).
            </span>
          </label>
          <div class="rn-actions">
            <pp-button variant="primary" size="sm" (click)="doRename()"
              >Save</pp-button
            >
            <pp-button variant="ghost" size="sm" (click)="cancelRename()"
              >Cancel</pp-button
            >
          </div>
        </div>
      </div>
    }
  `,
  styles: [
    `
      :host {
        display: block;
        padding: 1.25rem;
        color: var(--color-text);
      }
      h1 {
        margin: 0;
        font-size: 1.4rem;
      }
      .pf {
        margin-top: 1rem;
        max-width: 760px;
      }
      .pf__head {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        margin-bottom: 0.5rem;
      }
      .spacer {
        flex: 1;
      }
      .pf-row {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.5rem 0.4rem;
        border-bottom: 1px solid var(--color-border);
      }
      .pf-row:hover {
        background: var(--color-node);
      }
      .pf-row__main {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: flex-start;
        gap: 2px;
        background: none;
        border: none;
        color: inherit;
        cursor: pointer;
        text-align: left;
      }
      .pf-pill {
        font-size: 0.72rem;
        padding: 1px 7px;
        border-radius: 999px;
        border: 1px solid var(--color-border);
        white-space: nowrap;
      }
      .pf-pill--on {
        color: var(--color-pass, #2e7d32);
        border-color: currentColor;
      }
      .pf-del {
        background: none;
        border: none;
        color: var(--color-muted);
        cursor: pointer;
        padding: 4px;
        border-radius: 6px;
      }
      .pf-del:hover {
        color: var(--color-fail, #c0392b);
        background: var(--color-node);
      }
      .muted {
        color: var(--color-muted);
      }
      .rn-back {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.35);
        display: grid;
        place-items: center;
        z-index: 50;
      }
      .rn {
        background: var(--color-bg);
        color: var(--color-text);
        border: 1px solid var(--color-border);
        border-radius: 12px;
        padding: 18px 20px;
        width: min(440px, 92vw);
      }
      .rn h3 {
        margin: 0 0 6px;
      }
      .rn-fld {
        display: flex;
        flex-direction: column;
        gap: 4px;
        margin: 12px 0;
      }
      .rn-fld input {
        padding: 8px 10px;
        border: 1px solid var(--color-border);
        border-radius: 8px;
        background: var(--color-bg);
        color: var(--color-text);
      }
      .rn-chk {
        display: flex;
        gap: 8px;
        font-size: 0.85rem;
        margin-bottom: 14px;
      }
      .rn-actions {
        display: flex;
        gap: 8px;
        justify-content: flex-end;
      }
    `,
  ],
})
export class ParticipantsComponent implements OnInit {
  readonly svc = inject(ParticipantsService);
  private readonly ui = inject(UiService);
  private readonly router = inject(Router);

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.svc.reloadFlows();
    this.svc.reloadRunning();
  }

  /** Open the visual canvas editor for a flow (or a new one). */
  openCanvas(id?: string): void {
    this.router.navigate(["/flow-editor", id ?? "new"]);
  }

  readonly renaming = signal<{
    flow: ParticipantFlow;
    name: string;
    replace: boolean;
  } | null>(null);

  startRename(f: ParticipantFlow): void {
    this.renaming.set({ flow: f, name: f.name, replace: true });
  }

  cancelRename(): void {
    this.renaming.set(null);
  }

  doRename(): void {
    const rn = this.renaming();
    if (!rn || !rn.name.trim()) return;
    this.svc.duplicate(rn.flow.id, rn.name.trim(), rn.replace).subscribe({
      next: (res) => {
        const repointed = res.repointed_network_flows.length;
        this.ui.toast(
          "success",
          rn.replace
            ? `Renamed to “${res.flow.name}”` +
                (repointed ? ` · ${repointed} network flow(s) repointed` : "")
            : `Duplicated as “${res.flow.name}”`,
        );
        this.renaming.set(null);
        this.refresh();
        if (rn.replace) this.router.navigate(["/flow-editor", res.flow.id]);
      },
      error: (e) =>
        this.ui.toast("error", e?.error?.detail ?? "Rename failed."),
    });
  }

  async remove(f: ParticipantFlow): Promise<void> {
    if (!(await this.ui.confirm(`Delete flow “${f.name}”?`, { danger: true })))
      return;
    this.svc.remove(f.id).subscribe({
      next: () => this.ui.toast("success", "Deleted."),
      error: () => this.ui.toast("error", "Delete failed."),
    });
  }

  start(flowId: string): void {
    this.svc.start(flowId).subscribe({
      next: (r) => this.ui.toast("success", `Listening on :${r.port}`),
      error: (e) =>
        this.ui.toast("error", e?.error?.detail ?? "Could not start flow."),
    });
  }

  stop(pid: string): void {
    this.svc.stop(pid).subscribe({
      next: () => this.ui.toast("success", "Stopped."),
      error: () => this.ui.toast("error", "Could not stop."),
    });
  }
}

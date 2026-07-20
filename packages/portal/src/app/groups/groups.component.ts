import {
  Component,
  OnInit,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";

import { UiService } from "../shared/ui.service";
import { ThemeService } from "../shared/theme.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { BadgeComponent } from "../shared/ui/badge.component";
import { CardComponent } from "../shared/ui/card.component";
import { GroupsService, ParticipantGroup } from "./groups.service";
import { ConnectionsService } from "../connections/connections.service";

const POLICIES = ["round_robin", "weighted", "random", "failover", "sticky"];
const POLICY_TONE: Record<string, string> = {
  round_robin: "info",
  weighted: "brand",
  random: "neutral",
  failover: "warning",
  sticky: "success",
};
const POLICY_HINT: Record<string, string> = {
  round_robin: "Members take turns, one request each.",
  weighted: "Traffic split proportional to member weights.",
  random: "Each request picks a member at random.",
  failover: "Always the first healthy member — order is priority.",
  sticky: "Same key always lands on the same member.",
};

interface GroupDraft {
  id?: string;
  name: string;
  description: string;
  members: { connection: string; weight: number }[];
  policy: string;
  key: string;
}

/**
 * Participant groups — several member connections acting as one logical
 * participant, with a selection policy. Two-pane manage page consistent with
 * the rest of the app, with a hub → members fan-out preview.
 */
@Component({
  selector: "app-groups",
  standalone: true,
  imports: [
    FormsModule,
    PageHeaderComponent,
    ButtonComponent,
    IconComponent,
    BadgeComponent,
    CardComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="gp">
      <pp-page-header
        subtitle="A fleet: several member connections under one name with a selection policy (round-robin, failover, weighted, sticky). Target the group from a step or a flow."
      >
        <pp-button variant="primary" icon="plus" (click)="newGroup()"
          >New group</pp-button
        >
        <button
          type="button"
          class="iconbtn"
          (click)="theme.toggle()"
          title="Toggle theme"
        >
          <pp-icon
            [name]="theme.theme() === 'dark' ? 'sun' : 'moon'"
            [size]="18"
          />
        </button>
      </pp-page-header>

      <div class="gp__body">
        <aside class="gp__list">
          <div class="gp__search">
            <pp-icon name="search" [size]="14" />
            <input
              type="text"
              placeholder="Filter groups…"
              [ngModel]="query()"
              (ngModelChange)="query.set($event)"
            />
            @if (query()) {
              <button class="iconbtn" title="Clear" (click)="query.set('')">
                <pp-icon name="x" [size]="13" />
              </button>
            }
          </div>
          <div class="gp__list-head">
            <span
              >{{ filtered().length }} of
              {{ svc.groups().length }} group(s)</span
            >
            <span class="spacer"></span>
            <button class="iconbtn" (click)="svc.reload()" title="Refresh">
              <pp-icon name="activity" [size]="16" />
            </button>
          </div>
          @for (g of filtered(); track g.id) {
            <button
              class="row"
              [class.active]="draft()?.id === g.id"
              (click)="edit(g)"
            >
              <div class="row__main">
                <span class="row__name">
                  <strong>{{ g.name }}</strong>
                  @if (familyOf(g); as fam) {
                    <small class="tag">{{ familyLabel(fam) }}</small>
                  }
                </span>
                <small class="muted row__sub">
                  <span>{{ (g.members || []).length }} member(s)</span>
                  @if (g.updated_at) {
                    <span class="dotsep"></span
                    ><span>{{ ago(g.updated_at) }}</span>
                  }
                </small>
                @if (g.description) {
                  <small class="muted row__desc">{{ g.description }}</small>
                }
              </div>
              <pp-badge [tone]="tone(g.selection?.policy)">{{
                g.selection?.policy || "round_robin"
              }}</pp-badge>
            </button>
          } @empty {
            @if (svc.groups().length) {
              <p class="muted pad">No groups match “{{ query() }}”.</p>
            } @else {
              <p class="muted pad">No groups yet. Create one to get started.</p>
            }
          }
        </aside>

        <section class="gp__editor">
          @if (draft(); as d) {
            <pp-card
              [heading]="d.id ? d.name || 'Group' : 'New group'"
              subheading="Several connections, one logical participant"
            >
              <div card-actions>
                @if (isDirty()) {
                  <span class="unsaved" title="Unsaved changes">unsaved</span>
                }
                @if (d.id) {
                  <button
                    class="iconbtn"
                    title="Duplicate group"
                    (click)="duplicate(d)"
                  >
                    <pp-icon name="copy" [size]="16" />
                  </button>
                }
                <pp-badge [tone]="tone(d.policy)">{{ d.policy }}</pp-badge>
              </div>

              <!-- hub → members fan-out -->
              <div class="gp__diagram">
                <div class="gp__hub">
                  <pp-icon name="group" [size]="16" />
                  {{ d.name || "group" }}
                  <small>{{ d.policy }}</small>
                </div>
                <div class="gp__spokes">
                  @for (m of d.members; track $index) {
                    <div
                      class="gp__spoke"
                      [class.gp__spoke--empty]="!m.connection"
                      [class.gp__spoke--off]="isDisabled(m.connection)"
                    >
                      <span class="gp__line"></span>
                      <span class="gp__chip">
                        @if (d.policy === "failover") {
                          <em class="prio" [class.prio--first]="$index === 0">{{
                            $index + 1
                          }}</em>
                        }
                        {{ m.connection || "(unset)" }}
                        @if (d.policy === "weighted" && m.connection) {
                          <em>{{ share(d, m) }}%</em>
                        }
                        @if (isDisabled(m.connection)) {
                          <em class="off">disabled</em>
                        }
                      </span>
                    </div>
                  } @empty {
                    <span class="muted">add members…</span>
                  }
                </div>
              </div>
              <p class="hint">{{ policyHint(d.policy) }}</p>

              <div class="grid2">
                <label class="fld"
                  ><span>Name</span>
                  <input type="text" [(ngModel)]="d.name" placeholder="Issuers"
                /></label>
                <label class="fld"
                  ><span>Selection policy</span>
                  <select [(ngModel)]="d.policy">
                    @for (p of policies; track p) {
                      <option [value]="p">{{ p }}</option>
                    }
                  </select></label
                >
              </div>
              <div class="grid2">
                <label class="fld"
                  ><span>Description</span>
                  <input
                    type="text"
                    [(ngModel)]="d.description"
                    placeholder="What this fleet is for"
                /></label>
                @if (d.policy === "sticky") {
                  <label class="fld"
                    ><span
                      >Sticky key
                      <small class="muted"
                        >(request field, e.g. de.2)</small
                      ></span
                    >
                    <input type="text" [(ngModel)]="d.key" placeholder="pan"
                  /></label>
                }
              </div>

              <div class="members">
                <div class="members__head">
                  <span
                    >Members
                    <small class="muted">(connection × weight)</small></span
                  >
                  @if (groupFamily(d); as fam) {
                    <span class="badge">type: {{ familyLabel(fam) }}</span>
                  }
                  <pp-button
                    variant="ghost"
                    size="sm"
                    icon="plus"
                    (click)="addMember(d)"
                    >Add</pp-button
                  >
                </div>
                <p class="muted note">
                  A group is typed — all members must use the same adapter (only
                  matching connections are listed).
                  @if (d.policy === "failover") {
                    <strong
                      >Order is failover priority — first is primary.</strong
                    >
                  }
                </p>
                @for (m of d.members; track $index) {
                  <div
                    class="member"
                    [class.member--off]="isDisabled(m.connection)"
                  >
                    <span class="member__idx">{{ $index + 1 }}</span>
                    <select [(ngModel)]="m.connection">
                      <option value="">— connection —</option>
                      @for (
                        c of memberConnections(d, m.connection);
                        track c.name
                      ) {
                        <option [value]="c.name">
                          {{ c.name }}{{ c.disabled ? " (disabled)" : "" }}
                        </option>
                      }
                    </select>
                    <input
                      type="number"
                      min="1"
                      placeholder="w"
                      title="Weight (weighted policy)"
                      [(ngModel)]="m.weight"
                      [disabled]="d.policy !== 'weighted'"
                    />
                    <span class="member__ctl">
                      <button
                        class="iconbtn"
                        title="Move up"
                        [disabled]="$index === 0"
                        (click)="moveMember(d, $index, -1)"
                      >
                        <pp-icon name="chevron" [size]="14" class="rot180" />
                      </button>
                      <button
                        class="iconbtn"
                        title="Move down"
                        [disabled]="$index === d.members.length - 1"
                        (click)="moveMember(d, $index, 1)"
                      >
                        <pp-icon name="chevron" [size]="14" />
                      </button>
                      <button
                        class="iconbtn"
                        title="Remove"
                        (click)="removeMember(d, $index)"
                      >
                        <pp-icon name="x" [size]="15" />
                      </button>
                    </span>
                    @if (isDisabled(m.connection)) {
                      <small class="member__warn"
                        >out of rotation — connection is disabled</small
                      >
                    }
                  </div>
                } @empty {
                  <p class="muted">
                    No members yet — pick the connections in this fleet.
                  </p>
                }
              </div>

              <div class="actions">
                <pp-button variant="primary" icon="check" (click)="save()">{{
                  d.id ? "Save" : "Create"
                }}</pp-button>
                @if (d.id) {
                  <pp-button variant="ghost" (click)="remove(d)"
                    >Delete</pp-button
                  >
                }
                <span class="spacer"></span>
                <pp-button variant="ghost" (click)="close()">Close</pp-button>
              </div>
            </pp-card>
          } @else {
            <div class="empty">
              <pp-icon name="group" [size]="34" />
              <p class="muted">Select a group to edit, or create a new one.</p>
              <pp-button
                variant="ghost"
                size="sm"
                icon="plus"
                (click)="newGroup()"
                >New group</pp-button
              >
            </div>
          }
        </section>
      </div>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        color: var(--color-text);
      }
      .gp__body {
        display: grid;
        grid-template-columns: minmax(260px, 320px) 1fr;
        gap: 1rem;
        padding: 0 1.25rem 1.25rem;
      }
      .gp__list {
        border: 1px solid var(--color-border);
        border-radius: 12px;
        background: var(--color-card);
        overflow: auto;
        max-height: 78vh;
      }
      .gp__search {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 0.5rem 0.75rem;
        border-bottom: 1px solid var(--color-border);
        color: var(--color-muted);
      }
      .gp__search input {
        flex: 1;
        min-width: 0;
        border: none;
        background: none;
        color: var(--color-text);
        font: inherit;
        font-size: 0.85rem;
        outline: none;
      }
      .gp__search input::placeholder {
        color: var(--color-muted);
      }
      .gp__list-head {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 0.6rem 0.75rem;
        border-bottom: 1px solid var(--color-border);
        font-size: 0.8rem;
        color: var(--color-muted);
      }
      .spacer {
        flex: 1;
      }
      .row {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        width: 100%;
        padding: 0.6rem 0.75rem;
        border: none;
        border-bottom: 1px solid var(--color-border);
        background: none;
        color: inherit;
        cursor: pointer;
        text-align: left;
      }
      .row.active,
      .row:hover {
        background: var(--color-node);
      }
      .row.active {
        box-shadow: inset 2px 0 0 var(--color-brand, #4a6fa5);
      }
      .row__main {
        flex: 1;
        min-width: 0;
        display: flex;
        flex-direction: column;
        gap: 2px;
      }
      .row__name {
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }
      .row__sub {
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }
      .row__desc {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        font-size: 0.72rem;
      }
      .dotsep {
        width: 3px;
        height: 3px;
        border-radius: 50%;
        background: var(--color-muted);
      }
      .tag {
        font-size: 0.65rem;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        color: var(--color-muted);
        border: 1px solid var(--color-border);
        border-radius: 6px;
        padding: 1px 5px;
      }
      .muted {
        color: var(--color-muted);
      }
      .pad {
        padding: 1rem;
      }
      .iconbtn {
        background: none;
        border: none;
        color: var(--color-muted);
        cursor: pointer;
        padding: 4px;
        border-radius: 6px;
        display: inline-flex;
      }
      .iconbtn:hover:not(:disabled) {
        background: var(--color-node);
        color: var(--color-text);
      }
      .iconbtn:disabled {
        opacity: 0.35;
        cursor: default;
      }
      .rot180 {
        transform: rotate(180deg);
      }
      .unsaved {
        font-size: 0.7rem;
        color: var(--color-warning, #c78a2d);
        border: 1px solid currentColor;
        border-radius: 999px;
        padding: 2px 8px;
      }
      .gp__diagram {
        display: flex;
        align-items: center;
        gap: 1rem;
        padding: 0.9rem;
        margin-bottom: 0.35rem;
        border: 1px dashed var(--color-border);
        border-radius: 10px;
        background: var(--color-bg);
      }
      .gp__hub {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 2px;
        padding: 8px 14px;
        border: 1.5px solid var(--color-brand, #4a6fa5);
        border-radius: 12px;
        color: var(--color-brand, #4a6fa5);
        font-weight: 600;
        white-space: nowrap;
      }
      .gp__hub small {
        color: var(--color-muted);
        font-weight: 400;
        font-size: 0.7rem;
      }
      .gp__spokes {
        display: flex;
        flex-direction: column;
        gap: 5px;
      }
      .gp__spoke {
        display: flex;
        align-items: center;
      }
      .gp__line {
        width: 22px;
        height: 1px;
        background: var(--color-border);
      }
      .gp__chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 3px 10px;
        border: 1px solid var(--color-border);
        border-radius: 999px;
        background: var(--color-card);
        font-size: 0.8rem;
      }
      .gp__chip em {
        font-style: normal;
        font-size: 0.7rem;
        color: var(--color-muted);
      }
      .gp__chip .prio {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 15px;
        height: 15px;
        border-radius: 50%;
        border: 1px solid var(--color-border);
        font-size: 0.62rem;
      }
      .gp__chip .prio--first {
        border-color: var(--color-brand, #4a6fa5);
        color: var(--color-brand, #4a6fa5);
        font-weight: 600;
      }
      .gp__chip .off {
        color: var(--color-danger, #c0504d);
      }
      .gp__spoke--empty .gp__chip {
        border-style: dashed;
        color: var(--color-muted);
      }
      .gp__spoke--off .gp__chip {
        border-style: dashed;
        opacity: 0.7;
      }
      .hint {
        margin: 0 0 0.9rem;
        font-size: 0.75rem;
        color: var(--color-muted);
      }
      .grid2 {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 0.7rem;
        margin-bottom: 0.8rem;
      }
      .fld {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .fld input,
      .fld select,
      .member select,
      .member input {
        padding: 0.45rem 0.6rem;
        border: 1px solid var(--color-border);
        border-radius: 8px;
        background: var(--color-bg);
        color: var(--color-text);
      }
      .members {
        margin-bottom: 1rem;
      }
      .members__head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 0.5rem;
      }
      .note {
        font-size: 0.75rem;
        margin: 0 0 6px;
      }
      .member {
        display: grid;
        grid-template-columns: 22px 1fr 70px auto;
        gap: 6px;
        margin-bottom: 6px;
        align-items: center;
      }
      .member--off select {
        opacity: 0.65;
      }
      .member__idx {
        font-size: 0.7rem;
        color: var(--color-muted);
        text-align: center;
      }
      .member__ctl {
        display: inline-flex;
        align-items: center;
        gap: 2px;
      }
      .member__warn {
        grid-column: 2 / -1;
        font-size: 0.7rem;
        color: var(--color-warning, #c78a2d);
      }
      .actions {
        display: flex;
        align-items: center;
        gap: 0.5rem;
      }
      .empty {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 0.5rem;
        min-height: 260px;
        color: var(--color-muted);
        border: 1px dashed var(--color-border);
        border-radius: 12px;
      }
    `,
  ],
})
export class GroupsComponent implements OnInit {
  readonly svc = inject(GroupsService);
  readonly theme = inject(ThemeService);
  private readonly ui = inject(UiService);
  private readonly conns = inject(ConnectionsService);
  readonly policies = POLICIES;

  readonly draft = signal<GroupDraft | null>(null);
  readonly query = signal("");

  /** Serialized snapshot of the draft as loaded/saved — dirty = differs. */
  private baseline = "";

  readonly filtered = computed(() => {
    const q = this.query().trim().toLowerCase();
    const list = [...this.svc.groups()].sort((a, b) =>
      a.name.localeCompare(b.name),
    );
    if (!q) return list;
    return list.filter(
      (g) =>
        g.name.toLowerCase().includes(q) ||
        (g.description ?? "").toLowerCase().includes(q) ||
        (g.selection?.policy ?? "").includes(q),
    );
  });

  ngOnInit(): void {
    this.svc.reload();
    this.conns.reload();
  }

  tone(policy?: string): any {
    return POLICY_TONE[policy || "round_robin"] ?? "neutral";
  }

  policyHint(policy: string): string {
    return POLICY_HINT[policy] ?? "";
  }

  /** Relative "3d ago"-style timestamp for the list rows. */
  ago(iso: string): string {
    const ms = Date.now() - new Date(iso).getTime();
    if (!Number.isFinite(ms) || ms < 0) return "";
    const m = Math.floor(ms / 60000);
    if (m < 1) return "just now";
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  }

  /** Weighted policy: this member's share of total weight, as a percent. */
  share(d: GroupDraft, m: { weight: number }): number {
    const total = d.members.reduce((s, x) => s + (Number(x.weight) || 1), 0);
    return total ? Math.round(((Number(m.weight) || 1) / total) * 100) : 0;
  }

  /** Whether a member's connection is administratively disabled. */
  isDisabled(name: string): boolean {
    if (!name) return false;
    return (
      this.conns.connections().find((c) => c.name === name)?.disabled === true
    );
  }

  /** Adapter family of a connection — a group is typed to a single family.
   *  Keyed off the adapter first (payShield is an HSM; db probes are their own
   *  family), so a payShield connection isn't mislabelled "iso8583". */
  private connFamily(c: { adapter?: string; protocol?: string }): string {
    const a = c.adapter || "tcp";
    if (a === "grpc" || a === "http") return a;
    if (a === "payshield" || a === "hsm_client") return "header_echo";
    if (a.startsWith("db_probe")) return "db";
    return c.protocol || "iso8583";
  }

  /** Friendly label for the family badge. */
  familyLabel(fam: string): string {
    return (
      (
        {
          iso8583: "ISO 8583",
          header_echo: "HSM",
          grpc: "gRPC",
          http: "REST",
          db: "DB probe",
        } as Record<string, string>
      )[fam] ?? fam
    );
  }

  /** The family this group is locked to (from its first resolved member), or
   *  null when it has no members yet. */
  groupFamily(d: GroupDraft): string | null {
    for (const m of d.members) {
      const c = this.conns.connections().find((x) => x.name === m.connection);
      if (c) return this.connFamily(c);
    }
    return null;
  }

  /** A saved group's adapter family for the list row — its stamped
   *  ``adapter_type``, or derived from its first member for legacy groups. */
  familyOf(g: ParticipantGroup): string | null {
    // Derive from the actual members first so the badge is never stale — a group
    // whose stamped ``adapter_type`` predates the payShield/DB fix (and reads
    // "iso8583") still shows its real family.
    for (const m of g.members ?? []) {
      const c = this.conns.connections().find((x) => x.name === m.connection);
      if (c) return this.connFamily(c);
    }
    return g.adapter_type ?? null;
  }

  /** Connections offered as members: outbound only, and — once the group has a
   *  member — restricted to that same adapter family so it stays typed.
   *  ``current`` keeps a row's own (possibly now-filtered) pick listed so an
   *  existing selection never silently disappears from its dropdown. */
  memberConnections(d: GroupDraft, current?: string) {
    const fam = this.groupFamily(d);
    return this.conns
      .connections()
      .filter((c) => c.name === current || c.mode !== "inbound")
      .filter(
        (c) => c.name === current || fam == null || this.connFamily(c) === fam,
      );
  }

  // ---- dirty tracking -------------------------------------------------------

  private snapshot(d: GroupDraft | null): string {
    if (!d) return "";
    return JSON.stringify({
      name: d.name,
      description: d.description,
      policy: d.policy,
      key: d.key,
      members: d.members.map((m) => ({
        c: m.connection,
        w: Number(m.weight) || 1,
      })),
    });
  }

  isDirty(): boolean {
    return this.snapshot(this.draft()) !== this.baseline;
  }

  /** True when it is OK to drop the current draft (clean, or user confirmed). */
  private async confirmDiscard(): Promise<boolean> {
    if (!this.draft() || !this.isDirty()) return true;
    return this.ui.confirm("Discard unsaved changes to this group?", {
      confirmLabel: "Discard",
      danger: true,
    });
  }

  // ---- draft lifecycle ------------------------------------------------------

  async newGroup(): Promise<void> {
    if (!(await this.confirmDiscard())) return;
    const d: GroupDraft = {
      name: "",
      description: "",
      members: [],
      policy: "round_robin",
      key: "",
    };
    this.baseline = this.snapshot(d);
    this.draft.set(d);
  }

  async edit(g: ParticipantGroup): Promise<void> {
    if (this.draft()?.id === g.id) return;
    if (!(await this.confirmDiscard())) return;
    this.svc.get(g.id).subscribe({
      next: (full) => {
        const d: GroupDraft = {
          id: full.id,
          name: full.name,
          description: full.description ?? "",
          members: (full.members ?? []).map((m) => ({
            connection: m.connection,
            weight: m.weight ?? 1,
          })),
          policy: full.selection?.policy ?? "round_robin",
          key: full.selection?.key ?? "",
        };
        this.baseline = this.snapshot(d);
        this.draft.set(d);
      },
      error: () => this.ui.toast("error", "Could not load group."),
    });
  }

  /** Open a copy of the group as a new (unsaved) draft. */
  duplicate(d: GroupDraft): void {
    const copy: GroupDraft = {
      name: `${d.name} (copy)`,
      description: d.description,
      members: d.members.map((m) => ({ ...m })),
      policy: d.policy,
      key: d.key,
    };
    this.baseline = ""; // a copy is dirty by definition until created
    this.draft.set(copy);
  }

  async close(): Promise<void> {
    if (!(await this.confirmDiscard())) return;
    this.draft.set(null);
    this.baseline = "";
  }

  // ---- members --------------------------------------------------------------

  addMember(d: GroupDraft): void {
    d.members.push({ connection: "", weight: 1 });
  }

  removeMember(d: GroupDraft, i: number): void {
    d.members.splice(i, 1);
  }

  /** Reorder a member (order is failover priority). */
  moveMember(d: GroupDraft, i: number, delta: number): void {
    const j = i + delta;
    if (j < 0 || j >= d.members.length) return;
    const [m] = d.members.splice(i, 1);
    d.members.splice(j, 0, m);
  }

  // ---- persistence ----------------------------------------------------------

  save(): void {
    const d = this.draft();
    if (!d) return;
    if (!d.name.trim()) {
      this.ui.toast("error", "Give the group a name.");
      return;
    }
    const members = d.members.filter((m) => m.connection.trim());
    if (!members.length) {
      this.ui.toast("error", "Add at least one member connection.");
      return;
    }
    const seen = new Set<string>();
    for (const m of members) {
      const name = m.connection.trim();
      if (seen.has(name)) {
        this.ui.toast(
          "error",
          `Duplicate member “${name}” — each connection once.`,
        );
        return;
      }
      seen.add(name);
    }
    const selection: { policy: string; key?: string } = { policy: d.policy };
    if (d.policy === "sticky" && d.key.trim()) selection.key = d.key.trim();
    const body = {
      name: d.name.trim(),
      description: d.description,
      members: members.map((m) => ({
        connection: m.connection.trim(),
        weight: Math.max(1, Math.round(Number(m.weight) || 1)),
      })),
      selection,
    };
    const op = d.id ? this.svc.save(d.id, body) : this.svc.create(body);
    op.subscribe({
      next: (g) => {
        this.ui.toast("success", `Saved “${g.name}”`);
        this.draft.update((cur) => (cur ? { ...cur, id: g.id } : cur));
        this.baseline = this.snapshot(this.draft());
      },
      error: (e) => this.ui.toast("error", e?.error?.detail ?? "Save failed."),
    });
  }

  async remove(d: GroupDraft): Promise<void> {
    if (!d.id) return;
    if (!(await this.ui.confirm(`Delete group “${d.name}”?`, { danger: true })))
      return;
    this.svc.remove(d.id).subscribe({
      next: () => {
        this.ui.toast("success", "Deleted.");
        this.draft.set(null);
        this.baseline = "";
      },
      error: () => this.ui.toast("error", "Delete failed."),
    });
  }
}

import {
  Component,
  computed,
  signal,
  ChangeDetectionStrategy,
  inject,
  DestroyRef,
} from "@angular/core";
import { ActivatedRoute, RouterLink } from "@angular/router";

import { IconComponent } from "../shared/ui/icon.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import {
  ADAPTERS,
  getAdapter,
  availabilityLabel,
  type AdapterSpec,
} from "./adapter-catalog";

/** Per-category accent colour + icon, for the hero tile, section bars and chips. */
const CATEGORY_META: Record<string, { icon: string; accent: string }> = {
  Transport: { icon: "layers", accent: "#3b82f6" },
  Connection: { icon: "send", accent: "#8b5cf6" },
  "Host command": { icon: "shield", accent: "#14b8a6" },
  Routing: { icon: "flow", accent: "#ec4899" },
  Verification: { icon: "check", accent: "#22c55e" },
  Endpoint: { icon: "terminal", accent: "#f97316" },
  Utility: { icon: "settings", accent: "#64748b" },
};

interface SectionLink {
  id: string;
  label: string;
}

/**
 * Adapter detail — the full, source-derived specification for one adapter
 * (/adapters/:key). Reads the slug from the route, looks the adapter up in the
 * shared catalog (adapter-catalog.ts) and renders a category-accented hero, a
 * sticky quick-facts / on-this-page sidebar, and rich sections for overview,
 * protocol notes, config tables, actions, examples and error behaviour.
 */
@Component({
  selector: "app-adapter-detail",
  standalone: true,
  imports: [PageHeaderComponent, IconComponent, RouterLink],
  templateUrl: "./adapter-detail.component.html",
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: "./adapter-detail.component.scss",
})
export class AdapterDetailComponent {
  private readonly route = inject(ActivatedRoute);
  readonly availabilityLabel = availabilityLabel;

  /** Route slug — kept reactive so related-adapter links (which reuse this
   * component instance) re-render rather than showing the previous adapter. */
  private readonly slug = signal(this.route.snapshot.paramMap.get("key") ?? "");

  constructor() {
    const sub = this.route.paramMap.subscribe((p) =>
      this.slug.set(p.get("key") ?? ""),
    );
    inject(DestroyRef).onDestroy(() => sub.unsubscribe());
  }

  readonly adapter = computed(() => getAdapter(this.slug()));

  readonly meta = computed(() => {
    const a = this.adapter();
    return (a && CATEGORY_META[a.category]) || CATEGORY_META["Utility"];
  });

  readonly accent = computed(() => this.meta().accent);
  readonly categoryIcon = computed(() => this.meta().icon);

  /** Resolved seeAlso entries for related-adapter links. */
  readonly related = computed(() => {
    const a = this.adapter();
    if (!a?.seeAlso) return [];
    return a.seeAlso
      .map((s) => getAdapter(s))
      .filter((x): x is AdapterSpec => !!x);
  });

  /** "On this page" nav — only the sections this adapter actually has. */
  readonly sections = computed<SectionLink[]>(() => {
    const a = this.adapter();
    if (!a) return [];
    const out: SectionLink[] = [{ id: "overview", label: "Overview" }];
    if (a.protocolNotes?.length)
      out.push({ id: "protocol", label: "Protocol & framing" });
    for (const s of a.extraSections ?? [])
      out.push({ id: s.id, label: s.title });
    if (a.configGroups?.length)
      out.push({ id: "config", label: "Configuration" });
    if (a.actions?.length) out.push({ id: "actions", label: "Actions" });
    if (a.examples?.length) out.push({ id: "examples", label: "Examples" });
    if (a.errors?.length) out.push({ id: "errors", label: "Errors" });
    if (this.related().length) out.push({ id: "related", label: "Related" });
    return out;
  });

  metaIcon(category: string): string {
    return (CATEGORY_META[category] || CATEGORY_META["Utility"]).icon;
  }

  metaAccent(category: string): string {
    return (CATEGORY_META[category] || CATEGORY_META["Utility"]).accent;
  }

  scrollTo(id: string): void {
    document
      .getElementById(id)
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  readonly copied = signal<string | null>(null);

  copy(code: string, title: string): void {
    if (navigator?.clipboard) {
      navigator.clipboard.writeText(code).then(() => {
        this.copied.set(title);
        setTimeout(() => this.copied.set(null), 1500);
      });
    }
  }

  /** Fallback list when the slug is unknown. */
  readonly all = ADAPTERS;
}

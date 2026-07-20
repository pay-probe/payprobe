import {
  Component,
  computed,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { RouterLink } from "@angular/router";

import { IconComponent } from "../shared/ui/icon.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { ADAPTERS, availabilityLabel } from "./adapter-catalog";

/**
 * Adapters catalog — the connectors the worker uses to talk to payment
 * targets. Read-only reference: it mirrors the worker AdapterRegistry
 * (packages/worker/adapters/registry.py). Each card links to a detail page
 * (/adapters/:slug) with the full, source-derived spec. Real availability on a
 * given worker depends on its installed optional dependencies and the active
 * environment config.
 */
@Component({
  selector: "app-adapters",
  standalone: true,
  imports: [PageHeaderComponent, IconComponent, RouterLink],
  templateUrl: "./adapters.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./adapters.component.scss",
})
export class AdaptersComponent {
  readonly query = signal("");
  readonly availabilityLabel = availabilityLabel;

  private readonly all = ADAPTERS;

  readonly adapters = computed(() => {
    const q = this.query().trim().toLowerCase();
    if (!q) return this.all;
    return this.all.filter(
      (a) =>
        a.label.toLowerCase().includes(q) ||
        a.description.toLowerCase().includes(q) ||
        a.category.toLowerCase().includes(q) ||
        a.targets.some((t) => t.includes(q)),
    );
  });

  onSearch(value: string): void {
    this.query.set(value);
  }
}

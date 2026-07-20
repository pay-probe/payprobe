import {
  Component,
  ChangeDetectionStrategy,
  AfterViewInit,
  OnDestroy,
  signal,
  input,
} from "@angular/core";

export interface TocItem {
  id: string;
  label: string;
}

/**
 * "On this page" table of contents for a docs page. Renders a sticky list of
 * section links; clicking scrolls the section into view (the docs body is its
 * own scroll container, so native hash anchors don't work). An
 * IntersectionObserver highlights the section currently in view (scroll-spy).
 * Everything is defensive — if observation can't be set up, the links still
 * jump correctly.
 */
@Component({
  selector: "app-docs-toc",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <nav class="toc" aria-label="On this page">
      <div class="toc__h">On this page</div>
      @for (item of items(); track item.id) {
        <a
          class="toc__l"
          [class.on]="active() === item.id"
          (click)="go(item.id, $event)"
          [attr.href]="'#' + item.id"
          >{{ item.label }}</a
        >
      }
    </nav>
  `,
  styles: [
    `
      .toc {
        position: sticky;
        top: 8px;
        display: flex;
        flex-direction: column;
        gap: 2px;
        font-size: 12.5px;
        border-left: 1px solid var(--color-border);
        padding-left: 12px;
      }
      .toc__h {
        text-transform: uppercase;
        letter-spacing: 0.06em;
        font-size: 10px;
        font-weight: 700;
        color: var(--color-text-muted, var(--color-text));
        margin-bottom: 6px;
      }
      .toc__l {
        color: var(--color-text-muted, var(--color-text));
        text-decoration: none;
        padding: 4px 8px;
        border-radius: 6px;
        border-left: 2px solid transparent;
        margin-left: -13px;
        padding-left: 11px;
        line-height: 1.4;
      }
      .toc__l:hover {
        color: var(--color-text);
        background: var(--color-node, transparent);
      }
      .toc__l.on {
        color: var(--color-primary, #58a6ff);
        border-left-color: var(--color-primary, #58a6ff);
        font-weight: 600;
      }
    `,
  ],
})
export class DocsTocComponent implements AfterViewInit, OnDestroy {
  readonly items = input<TocItem[]>([]);
  readonly active = signal("");
  private observer: IntersectionObserver | null = null;

  ngAfterViewInit(): void {
    // Defer so the parent's sections are in the DOM and laid out.
    setTimeout(() => this.spy(), 0);
  }

  ngOnDestroy(): void {
    this.observer?.disconnect();
  }

  go(id: string, ev: Event): void {
    ev.preventDefault();
    const el =
      typeof document !== "undefined" ? document.getElementById(id) : null;
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
    this.active.set(id);
  }

  private spy(): void {
    if (
      typeof IntersectionObserver === "undefined" ||
      typeof document === "undefined"
    ) {
      return;
    }
    const els = this.items()
      .map((i) => document.getElementById(i.id))
      .filter((e): e is HTMLElement => !!e);
    if (!els.length) return;
    const root = this.scrollParent(els[0]);
    try {
      this.observer = new IntersectionObserver(
        (entries) => {
          for (const e of entries) {
            if (e.isIntersecting) this.active.set((e.target as HTMLElement).id);
          }
        },
        { root, rootMargin: "0px 0px -72% 0px", threshold: 0 },
      );
      els.forEach((el) => this.observer!.observe(el));
      this.active.set(els[0].id);
    } catch {
      this.observer = null;
    }
  }

  private scrollParent(el: HTMLElement): HTMLElement | null {
    let node: HTMLElement | null = el.parentElement;
    while (node) {
      const oy = getComputedStyle(node).overflowY;
      if (oy === "auto" || oy === "scroll") return node;
      node = node.parentElement;
    }
    return null;
  }
}

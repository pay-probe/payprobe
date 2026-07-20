import {
  Directive,
  ElementRef,
  HostListener,
  Input,
  OnDestroy,
  inject,
} from "@angular/core";

/**
 * Inline `${...}` reference autocomplete for any text input / textarea.
 *
 * Typing `${` (or editing inside an unclosed `${partial`) pops a filtered
 * dropdown of available references — upstream step response fields, `vars.*`
 * and `tables.*` — supplied via the directive input. Selecting one inserts the
 * full `${...}` token and fires an `input` event so ngModel stays in sync.
 *
 *   <input [appRefAutocomplete]="allRefs()" ... />
 */
@Directive({
  selector: "[appRefAutocomplete]",
  standalone: true,
})
export class RefAutocompleteDirective implements OnDestroy {
  @Input("appRefAutocomplete") refs: string[] = [];

  private readonly host =
    inject<ElementRef<HTMLInputElement | HTMLTextAreaElement>>(ElementRef);
  private panel: HTMLElement | null = null;
  private matches: string[] = [];
  private active = 0;
  private tokenStart = -1;

  @HostListener("input")
  @HostListener("click")
  onInput(): void {
    this.update();
  }

  @HostListener("keydown", ["$event"])
  onKeydown(e: KeyboardEvent): void {
    if (!this.panel) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      this.move(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      this.move(-1);
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      this.accept();
    } else if (e.key === "Escape") {
      e.stopPropagation();
      this.close();
    }
  }

  @HostListener("blur")
  onBlur(): void {
    setTimeout(() => this.close(), 150);
  }

  private inner(ref: string): string {
    return ref.replace(/^\$\{/, "").replace(/\}$/, "");
  }

  private update(): void {
    const input = this.host.nativeElement;
    const caret = input.selectionStart ?? input.value.length;
    const before = input.value.slice(0, caret);
    const m = /\$\{([^}\s]*)$/.exec(before);
    if (!m || !this.refs.length) {
      this.close();
      return;
    }
    this.tokenStart = caret - m[0].length;
    const q = m[1].toLowerCase();
    this.matches = this.refs
      .filter((r) => this.inner(r).toLowerCase().includes(q))
      .slice(0, 40);
    if (!this.matches.length) {
      this.close();
      return;
    }
    this.active = 0;
    this.render();
  }

  private render(): void {
    if (!this.panel) {
      this.panel = document.createElement("div");
      this.panel.className = "ref-ac";
      document.body.appendChild(this.panel);
    }
    const rect = this.host.nativeElement.getBoundingClientRect();
    this.panel.style.left = `${rect.left}px`;
    this.panel.style.top = `${rect.bottom + 2}px`;
    this.panel.style.minWidth = `${Math.max(rect.width, 220)}px`;
    this.panel.innerHTML = this.matches
      .map(
        (r, i) =>
          `<div class="ref-ac__item${i === this.active ? " ref-ac__item--active" : ""}" data-i="${i}">${this.esc(r)}</div>`,
      )
      .join("");
    this.panel.querySelectorAll<HTMLElement>(".ref-ac__item").forEach((el) => {
      el.addEventListener("mousedown", (ev) => {
        ev.preventDefault();
        this.active = Number(el.dataset["i"]);
        this.accept();
      });
    });
  }

  private move(d: number): void {
    this.active = (this.active + d + this.matches.length) % this.matches.length;
    this.render();
  }

  private accept(): void {
    const input = this.host.nativeElement;
    const ref = this.matches[this.active];
    const caret = input.selectionStart ?? input.value.length;
    const value = input.value;
    let end = caret;
    if (value[end] === "}") end++; // swallow an existing closing brace
    input.value = value.slice(0, this.tokenStart) + ref + value.slice(end);
    const newCaret = this.tokenStart + ref.length;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    setTimeout(() => {
      input.focus();
      input.selectionStart = input.selectionEnd = newCaret;
    });
    this.close();
  }

  private close(): void {
    this.panel?.remove();
    this.panel = null;
  }

  private esc(s: string): string {
    return s.replace(
      /[&<>]/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]!,
    );
  }

  ngOnDestroy(): void {
    this.close();
  }
}

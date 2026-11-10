import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
} from "@angular/core";

import { IconComponent } from "../../shared/ui/icon.component";
import { DeTable } from "./de-values.model";
import {
  StepFramePreview,
  frameBlocks,
  printable,
  sentRows,
  showSpaces,
  spacedHex,
} from "./frame-preview.model";

/**
 * The exact bytes a message step would put on the wire: the frame's parts
 * (length, TPDU, header, message, trailer) as colored hex, the message as text
 * with spaces made visible, and each sent field after padding. Dumb: the
 * editor makes the call and passes the result or the error in.
 */
@Component({
  selector: "app-frame-preview",
  standalone: true,
  imports: [IconComponent],
  templateUrl: "./frame-preview.component.html",
  styleUrl: "./frame-preview.component.scss",
  changeDetection: ChangeDetectionStrategy.Default,
})
export class FramePreviewComponent {
  readonly result = input<StepFramePreview | null>(null);
  readonly error = input<string | null>(null);
  readonly loading = input<boolean>(false);
  readonly table = input<DeTable>({});
  readonly refresh = output<void>();
  readonly closed = output<void>();

  readonly copied = signal(false);

  readonly blocks = computed(() => {
    const r = this.result();
    return r ? frameBlocks(r) : [];
  });

  readonly rows = computed(() => {
    const r = this.result();
    return r ? sentRows(r) : [];
  });

  readonly bodyText = computed(() => printable(this.result()?.body_hex ?? ""));

  spaced(hex: string): string {
    return spacedHex(hex);
  }

  visible(v: string): string {
    return showSpaces(v);
  }

  name(de: string): string {
    return this.table()[de]?.name ?? "";
  }

  async copyHex(): Promise<void> {
    const hex = this.result()?.hex;
    if (!hex) return;
    try {
      await navigator.clipboard.writeText(hex.toUpperCase());
      this.copied.set(true);
      setTimeout(() => this.copied.set(false), 1500);
    } catch {
      this.copied.set(false);
    }
  }
}

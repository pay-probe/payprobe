import { Injectable, signal } from "@angular/core";

export interface Toast {
  id: number;
  kind: "success" | "error";
  text: string;
}

export interface ConfirmRequest {
  title: string;
  message: string;
  confirmLabel: string;
  danger: boolean;
  resolve: (confirmed: boolean) => void;
}

/** App-wide toasts and confirm dialogs (rendered by UiOverlayComponent). */
@Injectable({ providedIn: "root" })
export class UiService {
  private nextId = 0;

  readonly toasts = signal<Toast[]>([]);
  readonly confirmRequest = signal<ConfirmRequest | null>(null);

  toast(kind: Toast["kind"], text: string): void {
    const toast: Toast = { id: ++this.nextId, kind, text };
    this.toasts.update((list) => [...list, toast]);
    setTimeout(() => this.dismiss(toast.id), kind === "error" ? 6000 : 3500);
  }

  dismiss(id: number): void {
    this.toasts.update((list) => list.filter((t) => t.id !== id));
  }

  confirm(
    message: string,
    options: { title?: string; confirmLabel?: string; danger?: boolean } = {},
  ): Promise<boolean> {
    return new Promise((resolve) => {
      this.confirmRequest.set({
        title: options.title ?? "Are you sure?",
        message,
        confirmLabel: options.confirmLabel ?? "Confirm",
        danger: options.danger ?? false,
        resolve,
      });
    });
  }

  resolveConfirm(confirmed: boolean): void {
    this.confirmRequest()?.resolve(confirmed);
    this.confirmRequest.set(null);
  }
}
